import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import git
import pytest
from pydantic import BaseModel, PrivateAttr

from is_user_codeowner_action.app import (
    AppContext,
    Codeowner,
    all_paths_owned,
    get_result,
    path_is_eligible,
    reduce_diff_paths,
)


@dataclass
class MockDiff:
    a_path: Optional[str] = None
    b_path: Optional[str] = None


class MockCommit(BaseModel):
    # The changes dict's keys are path names, and values are file contents.
    # This will replace the existing changes before a commit is made.
    changes: Dict[str, str]
    message: str  # The commit message to apply; useful for self-documenting your test cases
    hash: Optional[str]  # Can be used for debugging, not otherwise useful


class RepoWrapper(BaseModel):
    path: Path
    _commits: List[MockCommit] = PrivateAttr()
    _repo: git.Repo = PrivateAttr()

    def __init__(self, **data):
        super().__init__(**data)
        self._repo = git.Repo.init(str(self.path))
        config = self._repo.config_writer()
        config.add_value("user", "email", "test@test.xyz")
        config.add_value("user", "name", "tester")
        config.write()
        config.release()
        self._repo.git.commit(m="initial", allow_empty=True)
        self._repo.git.branch(m="main")
        self._commits = []

    @property
    def repo(self) -> git.Repo:
        return self._repo

    @property
    def commits(self) -> List[MockCommit]:
        return self._commits

    def apply_change(self, path: str, contents: str):
        document = self.path / path
        path_dir = os.path.dirname(str(document))
        os.makedirs(path_dir, exist_ok=True)
        document.write_text(contents)
        self.repo.index.add([path])

    def commit_change(self, message: str):
        self.repo.index.commit(message)

    def add_commit(self, commit: MockCommit):
        self.commits.append(commit)
        for path, data in commit.changes.items():
            self.apply_change(path, data)
        self.commit_change(commit.message)


class Commits:
    update_config_settings = MockCommit(
        message="Update config settings",
        changes={"config/settings.yml": "# something changed!"},
    )

    update_readme = MockCommit(
        message="Update README", changes={"README.md": "# Welcome!"}
    )

    codeowners = MockCommit(
        message="Add CODEOWNERS",
        changes={
            "CODEOWNERS": (
                "docs/*.md @doc-owner\n"
                "src/ @be-owner @stack-owner\n"
                "frontend/ @fe-owner @stack-owner\n"
                "config/ @fe-owner @be-owner @stack-owner\n"
            )
        },
    )

    frontend_change = MockCommit(
        message="Fix a frontend bug",
        changes={"frontend/app.js": "// code that doesn't exist can't break"},
    )

    backend_change = MockCommit(
        message="Fix a backend bug",
        changes={"src/app.py": "# code that doesn't exist can't break"},
    )

    mixed_code_change = MockCommit(
        message="Add a smiley face to all source code.",
        changes={
            "src/app.py": "# code that doesn't exist can't break",
            "frontend/app.js": "// code that doesn't exist can't break",
        },
    )

    codeowners_change = MockCommit(
        message="Update CODEOWNERS", changes={"CODEOWNERS": "* @hacker"}
    )


def create_git_repo(repo_path: Path, commits: List[MockCommit]) -> RepoWrapper:
    repo = RepoWrapper(path=repo_path)
    for commit in commits:
        repo.add_commit(commit)
    return repo


class TestCodeownerPath:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ("# invalid line", ValueError),
            ("", ValueError),
            (
                "*      @foo @bar  # baz",
                Codeowner(users=["@foo", "@bar"], pattern="*"),
            ),  # Default codeowners
            (
                "foo/bar/baz/*  @user1 @user2",
                Codeowner(users=["@user1", "@user2"], pattern="foo/bar/baz/*"),
            ),
        ],
    )
    def test_parse_line(self, line, expected):
        try:
            result = Codeowner.parse_line(line)
            assert result == expected
        except Exception as e:
            if inspect.isclass(expected) and isinstance(e, expected):
                return
            raise e

    @pytest.mark.parametrize(
        "codeowner, cases",
        [
            (
                Codeowner(pattern="foo/bar", users=["a"]),
                {
                    "foo/bar": True,
                    "foo/bar/baz.py": False,
                    "foo/bar/baz/bip/bop/blam/zip/zap.yaml": False,
                    "foo/bat/baz.py": False,
                    "boo/bar/baz.py": False,
                    "foo/bart": False,
                    "foo/bar/": False,
                },
            ),
            (
                Codeowner(pattern="*.js", users=["a"]),
                {
                    "foo.js": True,
                    "foo/bar/baz.js": True,
                    "foo.py": False,
                    "foo/bar/baz.py": False,
                },
            ),
            (
                Codeowner(pattern="foo/bar/*.js", users=["a"]),
                {
                    "foo.js": False,
                    "foo/bar/baz.js": True,
                    "foo.py": False,
                    "foo/bar/baz.py": False,
                },
            ),
            (
                Codeowner(pattern="foo/bar/", users=["a"]),
                {
                    "foo/bar/baz": True,
                    "foo/bar/baz.py": True,
                    "foo/bar/ba": True,
                    "foo/bar": False,
                    "foo/bar/baz/bip.py": True,
                },
            ),
            (
                Codeowner(pattern="foo/bar/*", users=["a"]),
                {
                    "foo/bar/baz": True,
                    "foo/bar/baz.py": True,
                    "foo/bar/ba": True,
                    "foo/bar": False,
                    "foo/bar/baz/bip.py": False,
                },
            ),
        ],
    )
    def test_includes(self, codeowner, cases):
        for path, expected in cases.items():
            assert (
                codeowner.includes(path) == expected
            ), f"{codeowner.pattern} is{' not' if not expected else ''} expected to include {path}"


class CodeOwnersTestBase:
    @pytest.fixture(autouse=True)
    def initialize(self):
        self.codeowners = [
            Codeowner(pattern="README.md", users=["@a"]),
            Codeowner(pattern="docs/*.md", users=["@a"]),
        ]


class TestPathIsEligible(CodeOwnersTestBase):
    @pytest.mark.parametrize(
        "path, expected",
        [
            ("README.md", True),
            ("README.txt", False),
            ("docs/onboarding.md", True),
            ("docs/onboarding.example", False),
            ("foo.bar", False),
        ],
    )
    def test_path_is_eligible(self, path, expected):
        assert path_is_eligible(path, self.codeowners) == expected


class TestAllPathsOwned(CodeOwnersTestBase):
    @pytest.mark.parametrize(
        "paths, expected",
        [
            (["README.md", "docs/onboarding.md", "docs/tips.md"], True),
            (["README.txt", "README.md"], False),
            (["/README.md", "README.md"], False),
            (["docs/examples/example-app.py", "docs/onboarding.md"], False),
            ([], False),
        ],
    )
    def test_all_paths_owned(self, paths, expected):
        assert all_paths_owned(self.codeowners, paths, "@user") == expected

    def test_empty_codeowners(self):
        self.codeowners = []
        assert not all_paths_owned(self.codeowners, ["README.md"], "@user")


class TestReduceDiffPaths:
    def test_reduce_diff_paths(self):
        diffs = [
            MockDiff(a_path="a", b_path="b"),
            MockDiff(a_path="a", b_path="a"),
            MockDiff(a_path="b", b_path="b"),
            MockDiff(a_path="c", b_path="a"),
        ]
        assert reduce_diff_paths(diffs) == {"a", "b", "c"}


class TestRunApp:
    @pytest.mark.parametrize(
        "commits, expected_results",
        [
            (
                [Commits.update_config_settings],
                {  # All devs own the configuration
                    "@doc-owner": False,
                    "@be-owner": True,
                    "@fe-owner": True,
                    "@stack-owner": True,
                },
            ),
            (
                [Commits.update_readme],
                {  # The README has no owner
                    "@doc-owner": False,
                    "@be-owner": False,
                    "@fe-owner": False,
                    "@stack-owner": False,
                },
            ),
            (
                [Commits.mixed_code_change],
                {
                    "@doc-owner": False,
                    "@be-owner": False,
                    "@fe-owner": False,
                    "@stack-owner": True,
                },
            ),
            (
                [Commits.frontend_change],
                {
                    "@doc-owner": False,
                    "@be-owner": False,
                    "@fe-owner": True,
                    "@stack-owner": True,
                },
            ),
            (
                [Commits.backend_change],
                {
                    "@doc-owner": False,
                    "@be-owner": True,
                    "@fe-owner": False,
                    "@stack-owner": True,
                },
            ),
            (
                # Has two commits so that the codeowners change updates an existing path,
                # instead of becoming the initial codeowners commit. This ensures that the tool
                # uses the _target_ CODEOWNERS, instead of the source.
                [Commits.update_readme, Commits.codeowners_change],
                {
                    "@doc-owner": False,
                    "@be-owner": False,
                    "@fe-owner": False,
                    "@stack-owner": False,
                    "@hacker": False,
                },
            ),
        ],
    )
    def test_run(self, commits, expected_results, tmp_path):
        # Make sure that CODEOWNERS always comes before the changes under test
        if "CODEOWNERS" not in commits[0].changes:
            commits.insert(0, Commits.codeowners)

        repo_config = create_git_repo(tmp_path, commits[:-1])
        git_ = repo_config.repo.git
        git_.checkout(b="test")
        repo_config.add_commit(commits[-1])
        for user, expected_result in expected_results.items():
            context = AppContext(
                user=user, target_branch="main", path_to_repository=str(tmp_path)
            )
            result = get_result(context)
            assert result == expected_result, user

    @pytest.mark.parametrize("user", ("@hacker", "@be-owner"))
    def test_run_fails_duplicate_codeowners(self, tmp_path, user):
        commit = Commits.codeowners.copy()
        # Add a second CODEOWNERS to the repository to test that
        # action will fail if this conflict is encountered.
        commit.changes[".github/CODEOWNERS"] = "* @hacker\n"
        repo_config = create_git_repo(tmp_path, [commit])
        repo_config.add_commit(
            MockCommit(
                message="Am I the hacker?",
                changes={"src/app.py": "from hacked import pwned"},
            )
        )

        context = AppContext(
            user=user, target_branch="main", path_to_repository=str(tmp_path)
        )
        with pytest.raises(FileExistsError):
            get_result(context)

    def test_run_fails_no_codeowners(self, tmp_path):
        repo_config = create_git_repo(tmp_path, [Commits.update_readme])
        repo_config.repo.git.checkout(b="test")
        repo_config.add_commit(Commits.codeowners)
        context = AppContext(
            user="@doesnt-matter",
            target_branch="main",
            path_to_repository=str(tmp_path),
        )

        with pytest.raises(FileNotFoundError):
            get_result(context)
