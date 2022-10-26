#!/usr/bin/env python3


if __name__ == "__main__":
    import argparse
    import asyncio
    import logging
    import os.path
    import os
    import sys
    from typing import Any
    from typing import Coroutine
    from typing import Literal

    from bci_build.package import ALL_OS_VERSIONS
    from bci_build.package import OsVersion
    from staging.bot import BRANCH_NAME_ENVVAR_NAME
    from staging.bot import LOGGER
    from staging.bot import OS_VERSION_ENVVAR_NAME
    from staging.bot import OSC_USER_ENVVAR_NAME
    from staging.bot import StagingBot
    from staging.build_result import is_build_failed
    from staging.build_result import render_as_markdown

    ACTION_T = Literal[
        "rebuild",
        "create_project",
        "query_build_result",
        "commit_state",
        "scratch_build",
        "cleanup",
        "wait",
        "get_build_quality",
    ]

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--os-version",
        type=str,
        choices=[str(v) for v in ALL_OS_VERSIONS],
        nargs=1,
        default=[os.getenv(OS_VERSION_ENVVAR_NAME)],
        help=f"The OS version for which all actions shall be made. The value from the environment variable {OS_VERSION_ENVVAR_NAME} is used if not provided.",
    )
    parser.add_argument(
        "--osc-user",
        type=str,
        nargs=1,
        default=[os.getenv(OSC_USER_ENVVAR_NAME)],
        help=f"The username as who the bot should act. If not provided, then the value from the environment variable {OSC_USER_ENVVAR_NAME} is used.",
    )
    parser.add_argument(
        "--branch-name",
        "-b",
        type=str,
        nargs=1,
        default=[os.getenv(BRANCH_NAME_ENVVAR_NAME, "")],
        help=f"Name of the branch & worktree to which the changes should be pushed. If not provided, then either the value of the environment variable {BRANCH_NAME_ENVVAR_NAME} is used or the branch name is autogenerated.",
    )
    parser.add_argument(
        "--load",
        "-l",
        action="store_true",
        help=f"Load the settings from {StagingBot.DOTENV_FILE_NAME} and ignore the settings for --branch and --os-version",
    )
    parser.add_argument(
        "--from-stdin",
        "-f",
        action="store_true",
        help="Load the bot settings from a github comment passed via standard input",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="Set the verbosity of the logger to stderr",
    )

    def add_commit_message_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "-c",
            "--commit-message",
            help="Optional commit message to be used instead of the default ('Test build')",
            nargs=1,
            type=str,
            default=[""],
        )

    subparsers = parser.add_subparsers(dest="action")
    subparsers.add_parser("rebuild", help="Force rebuild the BCI test project")
    subparsers.add_parser("create_project", help="Create the staging project on OBS")
    cleanup_parser = subparsers.add_parser(
        "cleanup", help="Remove the branch in git and the staging project in OBS"
    )
    cleanup_parser.add_argument(
        "--no-cleanup-branch",
        help="Don't delete the local & remote branch.",
        action="store_true",
    )
    cleanup_parser.add_argument(
        "--no-cleanup-project",
        help="Don't delete the staging project on OBS.",
        action="store_true",
    )
    subparsers.add_parser(
        "query_build_result",
        help="Fetch the current build state and pretty print the results in markdown format",
    )

    commit_state_parser = subparsers.add_parser(
        "commit_state", help="commits the current state into a test branch"
    )
    add_commit_message_arg(commit_state_parser)

    scratch_build_parser = subparsers.add_parser(
        "scratch_build",
        help="commit all changes, create a test project and rebuild everything",
    )
    add_commit_message_arg(scratch_build_parser)

    wait_parser = subparsers.add_parser(
        "wait",
        help="Wait for the project on OBS to finish building (this can take a long time!)",
    )
    wait_parser.add_argument(
        "-t",
        "--timeout-sec",
        help="Timeout of the wait operation in seconds",
        nargs=1,
        type=int,
        default=[None],
    )
    subparsers.add_parser(
        "get_build_quality", help="Return 0 if the build succeeded or 1 if it failed"
    )

    loop = asyncio.get_event_loop()
    args = parser.parse_args()

    if args.load and args.from_stdin:
        raise RuntimeError("The --from-stdin and --load flags are mutually exclusive")

    if not args.action:
        raise RuntimeError("No action specified")

    if args.load:
        bot = loop.run_until_complete(StagingBot.from_env_file())
    elif args.from_stdin:
        comment = sys.stdin.read()
        bot = StagingBot.from_github_comment(comment, osc_username=args.osc_user[0])
    else:
        if not args.os_version or not args.os_version[0]:
            raise ValueError("No OS version has been set")

        os_version = OsVersion.parse(args.os_version[0])
        bot = StagingBot(
            os_version=os_version,
            branch_name=args.branch_name[0],
            osc_username=args.osc_user[0],
        )

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt="%(levelname)s: %(message)s"))

    LOGGER.addHandler(handler)
    if args.verbose > 0:
        LOGGER.setLevel((3 - min(args.verbose, 2)) * 10)
    else:
        LOGGER.setLevel("ERROR")

    loop.run_until_complete(bot.setup())

    try:
        action: ACTION_T = args.action
        coro: Coroutine[Any, Any, Any] | None = None

        if action == "rebuild":
            coro = bot.force_rebuild()

        elif action == "create_project":
            coro = bot.write_pkg_configs()

        elif action == "commit_state":
            coro = bot.write_all_build_recipes_to_branch(args.commit_message[0])

        elif action == "query_build_result":

            async def print_build_res():
                return render_as_markdown(await bot.fetch_build_results())

            coro = print_build_res()

        elif action == "scratch_build":

            async def _scratch():
                commit_or_none = await bot.scratch_build(args.commit_message[0])
                return commit_or_none or "No changes"

            coro = _scratch()

        elif action == "cleanup":
            coro = bot.remote_cleanup(
                branches=not args.no_cleanup_branch,
                obs_project=not args.no_cleanup_project,
            )

        elif action == "wait":

            async def _wait():
                return render_as_markdown(
                    await bot.wait_for_build_to_finish(timeout_sec=args.timeout_sec[0])
                )

            coro = _wait()

        elif action == "get_build_quality":

            async def _quality():
                build_res = await bot.wait_for_build_to_finish()
                if is_build_failed(build_res):
                    raise RuntimeError("Build failed!")
                return "Build succeded"

            coro = _quality()
        else:
            assert False, f"invalid action: {action}"

        assert coro is not None
        res = loop.run_until_complete(coro)
        if res:
            print(res)
    finally:
        loop.run_until_complete(bot.teardown())
