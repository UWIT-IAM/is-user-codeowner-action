import os
import re
from argparse import ArgumentParser
from typing import List, Optional, Set, cast

import git
from pydantic import BaseModel, Field

parser = ArgumentParser(
    "Given a --user and a --target-branch, determine if the user is a codeowner of "
    "all paths touched in the diff between HEAD and target AND if each of those paths "
    "is annotated with '# !auto-approve'"
)
parser.add_argument(
    "--user",
    "-u",
    type=str,
    required=True,
    help="The github username of the entity who initiated the workflow; "
    "see $GITHUB_ACTOR in https://docs.github.com/en/actions/reference/environment-variables",
)

parser.add_argument(
    "--target-branch",
    "-t",
    type=str,
    required=True,
    help="The branch that the code change is being merged into; "
    "see $GITHUB_BASE_REF in https://docs.github.com/en/actions/reference/environment-variables",
)
parser.add_argument(
    "--path-to-repository",
    "-p",
    type=str,
    required=False,
    default="/github/workspace",
    help="If your repository is mounted anywhere than the /github/workspace directory, use this option to direct "
    "the tool to the right place.",
)

# Parse paths in Codeowners that are marked with !auto-approve; only approve if
# __all__ paths in the commit match approved paths tagged with the user.

LINE_PATTERN = re.compile(
    # All characters except spaces makes the path, followed by at least one whitespace character; this is optional.
    r"((?P<pattern>[^\s@#]+)\s+)"
    r"(?P<users>(@[\w\-]+\s?)+)"  # And at least 1 instance of '@blah-blorp'
    r"(\s+#.*)?$"  # We end with either whitespaces leading to a comment, or the end of a line.
)


class AppContext(BaseModel):
    user: str
    target_branch: str
    path_to_repository: str


class Codeowner(BaseModel):
    pattern: str
    users: List[str] = Field(..., min_items=1)

    @classmethod
    def parse_line(cls, line: str):
        """
        Parses CODEOWNERS syntax in the form of:
            /some/path/foo/*  @user1 @user2  # Perhaps a comment
        """
        line = line.strip()
        match = re.match(LINE_PATTERN, line)
        if not match:
            raise ValueError("Line cannot be parsed as valid CODEOWNER syntax")

        groups = match.groupdict()
        pattern = groups.get("pattern")
        return cls(pattern=pattern, users=groups["users"].split())

    def includes(self, path) -> bool:
        if self.pattern == path:
            return True
        # The CODEOWNERS spec says that 'foo/*' will only match
        # files that are direct descendents of 'foo/',
        # i.e.: "foo/bar.yml" will match but "foo/bar/baz.yml" will not.
        elif self.pattern.endswith("/*"):  # foo/bar/*
            parent_directory = self.pattern[:-1]
            return os.path.relpath(parent_directory, path) == ".."
        # Something ending in / will match anything under that directory, recursively.
        elif self.pattern.endswith("/"):
            relpath = os.path.relpath(self.pattern, path)
            # Descendents of a directory will have a relpath of e.g., '../../../../'
            return all(map(lambda p: p == "..", relpath.split(os.path.sep)))
        # If something is trying to globally check for a file type, like: '*.js', just check the extension
        elif re.match(r"^\*\.\w+$", self.pattern):
            return path.endswith(self.pattern[1:])
        else:
            ft_match = re.match(
                r"^(?P<path_prefix>.*)\*(?P<extension>\.\w+)", self.pattern
            )
            if ft_match:
                groups = ft_match.groupdict()
                is_correct_type = path.endswith(groups["extension"])
                return (
                    is_correct_type
                    and os.path.relpath(groups["path_prefix"], path) == ".."
                )
        return False


def load_codeowners(
    user: str, repo_path: str, target_branch: str, filename: Optional[str] = None
) -> List[Codeowner]:
    repo = git.Repo(repo_path)
    repo.git.checkout(target_branch)
    try:
        if not filename:
            for path in (
                os.path.join(repo_path, "CODEOWNERS"),
                os.path.join(repo_path, ".github", "CODEOWNERS"),
            ):
                if os.path.exists(path):
                    # Make sure that nobody can sneak an unguarded shadow codeowners into the repo
                    if filename:
                        raise FileExistsError(
                            "Found more than one CODEOWNERS pattern in the repository! Aborting."
                        )
                    filename = path
            if not filename:
                raise FileNotFoundError(
                    f"No valid CODEOWNERS file found in {repo_path}"
                )
        else:
            filename = os.path.join(repo_path, filename)

        if not os.path.exists(filename):
            raise FileNotFoundError(filename)

        def is_valid(line: str):
            if not line:
                return False
            line = line.strip()
            return line and line[0] != "#" and line[0:2] != "* "

        with open(cast(str, filename)) as f:
            # Filter out paths that the given user is not eligible for.
            return list(
                filter(
                    lambda p: user in p.users,
                    [
                        # Trim all whitespace so we know for sure what the first character of each line is.
                        Codeowner.parse_line(ln.strip())
                        for ln in f.readlines()
                        if is_valid(ln)
                    ],
                )
            )
    finally:
        repo.git.checkout("@{-1}")  # switch to previous branch


def path_is_eligible(path: str, codeowners: List[Codeowner]) -> bool:
    for owned in codeowners:
        if owned.includes(path):
            return True
    return False


def all_paths_owned(codeowners: List[Codeowner], paths: List[str], user: str) -> bool:
    if not codeowners:  # The user was not listed as a codeowner on any paths
        msg = f"User {user} has no valid paths in CODEOWNERS"
        print(msg)
        return False

    if not paths:
        print("No paths were touched. There is nothing for the user to own.")
        return False

    for path in paths:
        if not path_is_eligible(path, codeowners):
            print(f"User {user} is not a CODEOWNER of path {path}")
            return False
        else:
            print(f"User {user} is a CODEOWNER of path {path}")
    return True


def reduce_diff_paths(diffs: List[git.Diff]) -> Set[str]:
    paths = set()
    for diff in diffs:
        paths.add(diff.a_path)
        paths.add(diff.b_path)
    return paths


def get_change_diffs(target_branch: str, repo_path: str) -> List[git.Diff]:
    head = git.Repo(repo_path).head.commit
    return head.diff(target_branch)


def get_result(context: AppContext):
    codeowners = load_codeowners(
        context.user, context.path_to_repository, target_branch=context.target_branch
    )
    print(
        f"User {context.user} is a CODEOWNER of the following patterns: {[c.pattern for c in codeowners]}"
    )
    diffs = get_change_diffs(context.target_branch, context.path_to_repository)
    print(f"Checking ownership of {len(diffs)} total diffs")
    # Everything after this point happens in memory; file system access
    # is no longer required after this point.
    touched_paths = reduce_diff_paths(diffs)
    return all_paths_owned(codeowners, list(touched_paths), context.user)


def run_action():  # pragma: no cover
    args = parser.parse_args()
    result = get_result(
        AppContext(
            user=args.user,
            target_branch=args.target_branch,
            path_to_repository=args.path_to_repository,
        )
    )
    print(f"::set-output name=result::{result}")
