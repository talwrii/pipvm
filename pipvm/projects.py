"""Reading a Python project's pyproject.toml as data.

Nothing in the file is ever executed: [build-system], which names the build backend that would run
the project's own code, is ignored. Only what the project declares (its dependencies) is read, so that those
can be installed from the index the ordinary way.
"""
from pathlib import Path
from typing import NamedTuple

from .util import Error


class Project(NamedTuple):
    directory: Path
    name: str
    dependencies: list
    optional: dict
    scripts: dict

    def requirements(self, extras=()):
        """The declared dependencies, plus those of the named extras."""
        wanted = list(self.dependencies)
        for extra in extras:
            if extra not in self.optional:
                raise Error(f"{self.directory / 'pyproject.toml'} has no extra called {extra!r} "
                            f"(it has: {', '.join(sorted(self.optional)) or 'none'})")
            wanted += self.optional[extra]
        return wanted

    def python_path(self):
        """Where the project's importable code lives: src/ if there is one, else the project directory."""
        return [str(self.directory / "src")] if (self.directory / "src").is_dir() else [str(self.directory)]


def _strings(value, what, path):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise Error(f"{path}: {what} must be a list of strings")
    return value


def read(directory):
    """The project in `directory`. Raises Error if it cannot be understood without running its build tool."""
    try:
        import tomllib
    except ImportError:
        raise Error("reading pyproject.toml needs Python 3.11 or newer (tomllib); pass --no-project and use --with")
    directory = Path(directory).resolve()
    path = directory / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise Error(f"cannot read {path}: {error}")
    except tomllib.TOMLDecodeError as error:
        raise Error(f"{path} is not valid TOML: {error}")
    project = data.get("project")
    if not isinstance(project, dict):
        raise Error(f"{path} has no [project] table, so its dependencies cannot be read without running its build "
                    f"tool. Use --with to name what to install, or --no-project")
    dynamic = project.get("dynamic", [])
    if "dependencies" in dynamic or "optional-dependencies" in dynamic:
        raise Error(f"{path} computes its dependencies when it is built (dynamic), which cannot be read without "
                    f"running its build tool. Use --with to name what to install, or --no-project")
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise Error(f"{path}: optional-dependencies must be a table")
    return Project(
        directory=directory,
        name=str(project.get("name", directory.name)),
        dependencies=_strings(project.get("dependencies", []), "dependencies", path),
        optional={name: _strings(items, f"optional-dependencies.{name}", path) for name, items in optional.items()},
        scripts={**project.get("scripts", {}), **project.get("gui-scripts", {})},
    )


def check_requirements(requirements):
    """Refuse anything that is not a plain index requirement. A string beginning with '-' would be taken by pip
    as an option (--index-url, -e, ...), and a direct reference (name @ url) would fetch from somewhere other
    than the index."""
    for item in requirements:
        text = item.strip()
        if not text or "\n" in item or "\r" in item:
            raise Error(f"not a requirement: {item!r}")
        if text.startswith("-"):
            raise Error(f"{item!r} looks like a pip option, not a requirement")
        if "@" in text.split(";")[0] or "://" in text or text.startswith((".", "/", "~")):
            raise Error(f"{item!r}: direct URLs and paths are not supported; only requirements from the package index")
    return requirements
