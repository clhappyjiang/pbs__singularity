# Copyright (c) 2022 Leiden University Medical Center
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import logging
import os
import shlex
import subprocess
import sys
from contextlib import ExitStack
from typing import Callable, Dict, List

from WDL import Type, Value
from WDL.runtime import config
from WDL.runtime.backend.cli_subprocess import _SubprocessScheduler
from WDL.runtime.backend.singularity import SingularityContainer


class SlurmSingularity(SingularityContainer):
    @classmethod
    def global_init(cls, cfg: config.Loader, logger: logging.Logger) -> None:
        # Set resources to maxsize. The base class (_SubProcessScheduler)
        # looks at the resources of the current host, but since we are
        # dealing with a cluster these limits do not apply.
        cls._resource_limits = {
            "cpu": sys.maxsize,
            "mem_bytes": sys.maxsize,
            "time": sys.maxsize,
        }
        _SubprocessScheduler.global_init(cls._resource_limits)
        # Since we run on the cluster, the images need to be placed in a
        # shared directory. The singularity cache itself cannot be shared
        # across nodes, as it can become corrupted when nodes pull the same
        # image. The solution is to pull image to a shared directory on the
        # submit node. If no image_cache is given, simply place a folder in
        # the current working directory.
        if cfg.get("singularity", "image_cache") == "":
            cfg.override(
                {"singularity": {
                    "image_cache": os.path.join(os.getcwd(),
                                                "miniwdl_singularity_cache")
                }}
            )
        SingularityContainer.global_init(cfg, logger)

    @classmethod
    def detect_resource_limits(cls, cfg: config.Loader,
                               logger: logging.Logger) -> Dict[str, int]:
        return cls._resource_limits  # type: ignore

    @property
    def cli_name(self) -> str:
        return "pbs_singularity"

    def process_runtime(self,
                        logger: logging.Logger,
                        runtime_eval: Dict[str, Value.Base]) -> None:
        """Any non-default runtime variables can be parsed here"""
        super().process_runtime(logger, runtime_eval)
        if "time_minutes" in runtime_eval:
            time_minutes = runtime_eval["time_minutes"].coerce(Type.Int()).value
            self.runtime_values["time_minutes"] = time_minutes

        if "pbs_account" in runtime_eval:
            slurm_account = runtime_eval["pbs_account"].coerce(
                Type.String()).value
            self.runtime_values["pbs_account"] = slurm_account

        if "pbs_account_gpu" in runtime_eval:
            slurm_account_gpu = runtime_eval["pbs_account_gpu"].coerce(
                Type.String()).value
            self.runtime_values["pbs_account_gpu"] = slurm_account_gpu

        if "slurm_partition" in runtime_eval:
            slurm_partition = runtime_eval["slurm_partition"].coerce(
                Type.String()).value
            self.runtime_values["slurm_partition"] = slurm_partition


        if "slurm_constraint" in runtime_eval:
            slurm_constraint = runtime_eval["slurm_constraint"].coerce(
                Type.String()).value
            self.runtime_values["slurm_constraint"] = slurm_constraint

    def _slurm_invocation(self):


        qsub_args = [
            "qsub",
            "#PBS  -N", self.run_id,
            "#PBS -q cgp",
            # Specifically state that only one task may be spawned.
            # This prevents issues with the --cpus-per-task flag.

        ]
        memory = self.runtime_values.get("memory_reservation", None)
        if memory is not None:
            # Round to the nearest megabyte.
            qsub_args.extend(["--mem", f"{round(memory / (1024 ** 2))}M"])



        if self.cfg.has_section("pbs"):
            extra_args = self.cfg.get("pbs", "extra_args")
            if extra_args is not None:
                qsub_args.extend(shlex.split(extra_args))
        # This is a script that simply executes all the following arguments.
        exec_script = os.path.join(os.path.dirname(__file__), "scripts",
                                   "exec_script.sh")
        qsub_args.append(exec_script)
        return qsub_args

    def _run_invocation(self, logger: logging.Logger, cleanup: ExitStack,
                        image: str) -> List[str]:
        singularity_command = super()._run_invocation(logger, cleanup, image)

        slurm_invocation = self._slurm_invocation()
        slurm_invocation.extend(singularity_command)
        logger.info("Slurm invocation: " + ' '.join(
            shlex.quote(part) for part in slurm_invocation))
        return slurm_invocation

    def _run(self,
             logger: logging.Logger,
             terminating: Callable[[], bool],
             command: str
             ) -> int:
        # Line copied from base class as value is not publicly exposed.
        cli_log_filename = os.path.join(self.host_dir, f"{self.cli_name}.log.txt")
        try:
            return super()._run(logger, terminating, command)
        finally:
            if terminating():  # Cancel the job if terminating
                with open(cli_log_filename, "rt") as submit_log:
                    # "job_id" or "job_id;cluster_name" are output with --parsable.
                    job_id, *clusters = submit_log.read().strip().split(";", maxsplit=1)
                if job_id.isdigit():  # A valid job ID.
                    scancel_args = ["scancel"]
                    if clusters:
                        scancel_args.append(f"--clusters={clusters[0]}")
                    subprocess.run(scancel_args + [job_id])