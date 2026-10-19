"""Start the packaged CLI with Apple's signed Python, independently of its install environment."""
import os
import sys


def main():
    if sys.platform != "darwin":
        raise SystemExit("TV Cast requires macOS.")
    try:
        os.execv("/usr/bin/python3", ["/usr/bin/python3", "-I", os.path.realpath(__file__)] + sys.argv[1:])
    except OSError as e:
        raise SystemExit("Cannot start Apple's Python: %s. Install the Command Line Tools with"
                         " xcode-select --install." % e) from e


if __name__ == "__main__":
    # Isolated Python cannot see a pip/uv environment's site-packages. The package is pure
    # Python with no runtime dependencies, so its own installation directory is sufficient.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    from tvcast.cli import main as cli_main

    cli_main()
