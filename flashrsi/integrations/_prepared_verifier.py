"""Use a preinstalled verifier venv and Harbor's unchanged scoring implementation."""

import copy
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from harbor.models.task.paths import TaskPaths
from harbor.verifier.verifier import Verifier

from flashrsi.integrations._verifier_dependencies import ROOT, dependency_spec


class PreparedVerifier(Verifier):
    def __init__(self, *, dependency_spec_sha256, **kwargs):
        super().__init__(**kwargs)
        self._dependency_spec_sha256 = dependency_spec_sha256

    async def verify(self):
        if self.step_name is not None or self._skip_tests_upload:
            raise ValueError(
                "Prepared verifier requires a single-step uploaded test suite"
            )
        source = self.task.paths.tests_dir
        self.task.paths.validate_input_tree(source)
        spec = dependency_spec((source / "test.sh").read_text())
        if spec["spec_sha256"] != self._dependency_spec_sha256:
            raise ValueError("Verifier script changed after dependency preparation")
        check = await self.environment.exec(f"cat {ROOT}/spec.sha256", user="root")
        if check.return_code or (check.stdout or "").strip() != spec["spec_sha256"]:
            raise ValueError("Container is missing its prepared verifier dependencies")
        with TemporaryDirectory(prefix="flashrsi-verifier-tests-") as temporary:
            prepared = Path(temporary) / "tests"
            shutil.copytree(source, prepared)
            (prepared / "test.sh").write_text(spec["prepared_script"])
            # Validate the copied input tree against its own temporary root;
            # never relax Harbor's path checks or mutate the shared native Task.
            original_task = self.task
            self.task = copy.copy(original_task)
            self.task.paths = TaskPaths(temporary)
            try:
                result = await super().verify()
                (
                    self.trial_paths.verifier_dir / "dependency-preparation.json"
                ).write_text(
                    json.dumps(
                        {k: v for k, v in spec.items() if k != "prepared_script"},
                        indent=2,
                    )
                )
                return result
            finally:
                self.task = original_task
