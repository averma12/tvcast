# Releasing

The version lives in `tvcast/__init__.py`. Releases contain a wheel and a source distribution;
both include the web panel and license notices. Runtime dependencies must remain empty because
the daemon runs with Apple's isolated Python, outside the installer's Python environment.

## One-time setup

In the PyPI account that will own the project, add a
[pending Trusted Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/):

| Field | Value |
|---|---|
| PyPI project | `tvcast` |
| GitHub owner | `thepushkarp` |
| Repository | `tvcast` |
| Workflow | `release.yml` |
| Environment | `pypi` |

Create the matching GitHub environment under repository Settings → Environments.
The workflow uses PyPI's Trusted Publishing; no API token belongs in the repository.
A pending publisher does not reserve the package name.

## Each release

1. Update `__version__`, run the tests, and merge the release changes after the Package check passes.
2. Build locally with `uv build` and check metadata with `uvx twine check dist/*`.
3. Publish a GitHub release from `main` with the matching tag, such as `v0.1.0`.
4. The Package workflow tests on macOS, builds from the source distribution, smoke-tests an
   installed wheel, and publishes those artifacts to PyPI. It rejects mismatched tags and versions.
5. Confirm the version on PyPI, then try `uvx --from tvcast==0.1.0 tv --version` from outside the checkout.

Only the GitHub release event publishes. Pull requests and pushes to `main` build and test
without uploading to PyPI. A published version cannot be overwritten; corrections need a new version.
