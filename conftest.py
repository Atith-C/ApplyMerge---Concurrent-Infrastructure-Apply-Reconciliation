"""The environment the test suite runs in — declared here, not inherited.

`build_world()` reads `.env`, which is right for the application and wrong for the
tests: with APPLYMERGE_BACKEND=github in a developer's .env the suite would quietly
start calling the real API — slow, rate-limited, and dependent on a network.

pytest imports this before any test module, and `load_dotenv()` does not override
variables that are already set, so these win.
"""

import os

os.environ["APPLYMERGE_BACKEND"] = "memory"
os.environ.pop("APPLYMERGE_REPO", None)

# Sign-in is exercised against a fake transport, so the tests must not depend on
# whether the machine running them happens to have real credentials configured.
os.environ.pop("GITHUB_CLIENT_ID", None)
os.environ.pop("GITHUB_CLIENT_SECRET", None)
