import ray

import warnings
from collections import defaultdict
from dataclasses import dataclass
import os
from typing import Dict, Callable, Any, Optional, List
import logging

from horovod.runner.common.util import secret, timeout, hosts
from horovod.runner.http.http_server import RendezvousServer
from horovod.ray.utils import detect_nics, nics_to_env_var, map_blocking
from horovod.ray.strategy import ColocatedStrategy, PackStrategy
logger = logging.getLogger(__name__)


@dataclass
class MiniSettings:
    """Minimal settings necessary for Ray to work.

    Can be replaced with a proper Horovod Settings object.
    """
    nics: set = None
    verbose: int = 1
    key: str = secret.make_secret_key() if secret else None
    ssh_port: int = None
    ssh_identity_file: str = None
    timeout_s: int = 300
    placement_group_timeout_s: int = 100

    @property
    def start_timeout(self):
        return timeout.Timeout(
            self.timeout_s,
            message="Timed out waiting for {activity}. Please "
            "check connectivity between servers. You "
            "may need to increase the --start-timeout "
            "parameter if you have too many servers.")


class Coordinator:
    """Responsible for instantiating the Rendezvous server.

    Args:
        settings: Horovod Settings object."""
    rendezvous = None
    global_rendezv_port = None
    nics = None
    hostnames = None

    def __init__(
            self,
            settings,
    ):
        self.settings = settings
        self.hostnames_by_rank = defaultdict(list)

    @property
    def world_size(self) -> int:
        return sum(len(ranks) for ranks in self.hostnames_by_rank.values())

    @property
    def hoststring(self) -> str:
        return ",".join([
            f"{host}:{len(ranks)}"
            for host, ranks in self.hostnames_by_rank.items()
        ])

    def register(self, hostname: str, world_rank: int):
        self.hostnames_by_rank[hostname].append(world_rank)

    def finalize_registration(self) -> dict:
        """Return a dictionary for all ranks."""
        rank_to_info = {}

        cross_sizes = defaultdict(int)
        cross_ranks = {}
        for rank_list in self.hostnames_by_rank.values():
            for local_rank, world_rank in enumerate(rank_list):
                cross_ranks[world_rank] = cross_sizes[local_rank]
                cross_sizes[local_rank] += 1

        for node_world_rank, (hostname, ranks) in enumerate(
                self.hostnames_by_rank.items()):
            for local_rank, world_rank in enumerate(ranks):
                rank_to_info[world_rank] = dict(
                    HOROVOD_CROSS_RANK=cross_ranks[world_rank],
                    HOROVOD_CROSS_SIZE=cross_sizes[local_rank],
                    HOROVOD_LOCAL_RANK=local_rank,
                    HOROVOD_LOCAL_SIZE=len(ranks))
        return rank_to_info

    def establish_rendezvous(self) -> Dict[str, str]:
        """Creates the rendezvous server and identifies the nics to be used.

        Returns:
            Environment variables for each worker.
        """

        # start global rendezvous server and get port that it is listening on
        self.rendezvous = RendezvousServer(self.settings.verbose)

        # allocate processes into slots
        # hosts = parse_hosts(hosts_string="10.11.11.11:4,10.11.11.12:4")
        parsed_hosts = hosts.parse_hosts(hosts_string=self.hoststring)
        host_alloc_plan = hosts.get_host_assignments(parsed_hosts,
                                                     self.world_size)

        # start global rendezvous server and get port that it is listening on
        self.global_rendezv_port = self.rendezvous.start()
        self.rendezvous.init(host_alloc_plan)

        return {
            "HOROVOD_GLOO_RENDEZVOUS_ADDR": ray.util.get_node_ip_address(),
            "HOROVOD_GLOO_RENDEZVOUS_PORT": str(self.global_rendezv_port),
            "HOROVOD_CONTROLLER": "gloo",
            "HOROVOD_CPU_OPERATIONS": "gloo",
        }


class RayExecutor:
    """Job class for Horovod + Ray integration.

    Args:
        settings (horovod.Settings): Configuration for job setup. You can
            use a standard Horovod Settings object or create one directly
            from RayExecutor.create_settings.
        num_workers (int): Number of workers to use for training.
        cpus_per_worker (int): Number of CPU resources to allocate to
            each worker.
        use_gpu (bool): Whether to use GPU for allocation. TODO: this
            can be removed.
        gpus_per_worker (int): Number of GPU resources to allocate to
            each worker.
        num_hosts (int): Alternative API to ``num_workers``. Number of
            machines to execute the job on. Used to enforce equal number of
            workers on each machine.
        num_workers_per_host (int): Alternative API to
            ``num_workers``. Number of workers to be placed on each machine.
            Used to enforce equal number of workers on each machine. Only
            used in conjunction with `num_hosts`.

    """

    @classmethod
    def create_settings(cls,
                        timeout_s,
                        ssh_identity_file=None,
                        ssh_str=None,
                        placement_group_timeout_s=100):
        """Create a mini setting object.

        Args:
            timeout_s (int): Timeout parameter for Gloo rendezvous.
            ssh_identity_file (str): Path to the identity file to
                ssh into different hosts on the cluster.
            ssh_str (str): CAUTION WHEN USING THIS. Private key
                file contents. Writes the private key to ssh_identity_file.
            placement_group_timeout_s (int): Timeout parameter for Ray
                Placement Group creation.

        Returns:
            MiniSettings object.
        """
        if ssh_str and not os.path.exists(ssh_identity_file):
            with open(ssh_identity_file, "w") as f:
                os.chmod(ssh_identity_file, 0o600)
                f.write(ssh_str)
        return MiniSettings(
            ssh_identity_file=ssh_identity_file,
            timeout_s=timeout_s,
            placement_group_timeout_s=placement_group_timeout_s)

    def __init__(
            self,
            settings,
            num_workers: Optional[int] = None,
            num_hosts: Optional[int] = None,
            num_workers_per_host: int = 1,
            cpus_per_worker: int = 1,
            use_gpu: bool = False,
            gpus_per_worker: Optional[int] = None,
            # Deprecated Args.
            num_slots: Optional[int] = None,
            cpus_per_slot: Optional[int] = None,
            gpus_per_slot: Optional[int] = None):

        if num_slots:
            warnings.warn(
                "`num_slots` is now deprecated. Please use the `num_workers` "
                "API, or to enforce an equal number of workers on each node, "
                "set `num_hosts` and `num_workers_per_host`. "
                "This will raise an error in a later release of Horovod. "
                "Setting num_workers_per_host = num_slots.",
                category=DeprecationWarning,
                stacklevel=2)
            num_workers_per_host = num_slots

        if cpus_per_slot or gpus_per_slot:
            warnings.warn(
                "`cpus_per_slot` and `gpus_per_slot` have been deprecated. "
                "Use `cpus_per_worker` and `gpus_per_worker` instead. "
                "This will raise an error in a later release of Horovod. "
                "Setting cpus/gpus_per_slot = cpus/gpus_per_worker.",
                category=DeprecationWarning,
                stacklevel=2)
            cpus_per_worker = cpus_per_slot
            gpus_per_worker = gpus_per_slot

        if num_workers is None and num_hosts is None:
            raise ValueError("Either `num_workers` or `num_hosts` must be "
                             "set.")

        if num_workers and num_hosts:
            raise ValueError("Both `num_workers` and `num_hosts` cannot be "
                             "set.")

        if gpus_per_worker and not use_gpu:
            raise ValueError("gpus_per_worker is set, but use_gpu is False. "
                             "use_gpu must be True if gpus_per_worker is "
                             "set. ")
        if use_gpu and isinstance(gpus_per_worker,
                                  int) and gpus_per_worker < 1:
            raise ValueError(
                f"gpus_per_worker must be >= 1: Got {gpus_per_worker}.")

        self.settings = settings
        self.num_workers = num_workers
        self.num_hosts = num_hosts
        self.num_workers_per_host = num_workers_per_host
        self.cpus_per_worker = cpus_per_worker
        self.use_gpu = use_gpu
        self.gpus_per_worker = gpus_per_worker or 1

        self.workers = []
        self.strategy = None

    def _start_executables(self, executable_cls, executable_args,
                           executable_kwargs):
        def _start_exec(worker):
            return worker.start_executable.remote(
                executable_cls, executable_args, executable_kwargs)

        map_blocking(_start_exec, self.workers)

    def _create_strategy(self):
        assert self.num_workers is None or self.num_hosts is None
        if self.num_workers:
            return PackStrategy(
                settings=self.settings,
                num_workers=self.num_workers,
                use_gpu=self.use_gpu,
                cpus_per_worker=self.cpus_per_worker,
                gpus_per_worker=self.gpus_per_worker)
        else:
            return ColocatedStrategy(
                settings=self.settings,
                num_hosts=self.num_hosts,
                num_workers_per_host=self.num_workers_per_host,
                use_gpu=self.use_gpu,
                cpus_per_worker=self.cpus_per_worker,
                gpus_per_worker=self.gpus_per_worker)

    def start(self,
              executable_cls: type = None,
              executable_args: Optional[List] = None,
              executable_kwargs: Optional[Dict] = None,
              extra_env_vars: Optional[Dict] = None):
        """Starts the workers and colocates them on all machines.

        We implement a node grouping because it seems like
        our implementation doesn't quite work for imbalanced nodes.
        Also, colocation performance is typically much better than
        non-colocated workers.

        Args:
            executable_cls (type): The class that will be created within
                an actor (BaseHorovodWorker). This will allow Horovod
                to establish its connections and set env vars.
            executable_args (List): Arguments to be passed into the
                worker class upon initialization.
            executable_kwargs (Dict): Keyword arguments to be passed into the
                worker class upon initialization.
            extra_env_vars (Dict): Environment variables to be set
                on the actors (worker processes) before initialization.

        """
        extra_env_vars = extra_env_vars or {}

        self.strategy = self._create_strategy()
        self.coordinator = Coordinator(self.settings)
        executable_args = executable_args or []
        self.workers, node_workers = self.strategy.create_workers()
        # Get all the hostnames of all workers
        hostnames = map_blocking(lambda w: w.hostname.remote(), self.workers)
        # Register each hostname to the coordinator. assumes the hostname
        # ordering is the same.
        for rank, hostname in enumerate(hostnames):
            self.coordinator.register(hostname, rank)
        all_info = self.coordinator.finalize_registration()

        indexed_runners = dict(enumerate(self.workers))
        for rank, local_cross_env_var in all_info.items():
            indexed_runners[rank].update_env_vars.remote(local_cross_env_var)

        coordinator_envs = self.coordinator.establish_rendezvous()
        coordinator_envs.update(extra_env_vars)
        nics = detect_nics(
            self.settings,
            all_host_names=list(self.coordinator.hostnames_by_rank),
            node_workers=node_workers)
        coordinator_envs.update(nics_to_env_var(nics))

        map_blocking(lambda w: w.update_env_vars.remote(coordinator_envs),
                     self.workers)

        self._start_executables(executable_cls, executable_args,
                                executable_kwargs)

    def execute(self, fn: Callable[["executable_cls"], Any]) -> List[Any]:
        """Executes the provided function on all workers.

        Args:
            fn: Target function to be invoked on every object.

        Returns:
            Deserialized return values from the target function.
        """
        return ray.get([worker.execute.remote(fn) for worker in self.workers])

    def run(self,
            fn: Callable[[Any], Any],
            args: Optional[List] = None,
            kwargs: Optional[Dict] = None) -> List[Any]:
        """Executes the provided function on all workers.

        Args:
            fn: Target function that can be executed with arbitrary
                args and keyword arguments.
            args: List of arguments to be passed into the target function.
            kwargs: Dictionary of keyword arguments to be
                passed into the target function.

        Returns:
            Deserialized return values from the target function.
        """
        return ray.get(self.run_remote(fn, args, kwargs))

    def run_remote(self,
                   fn: Callable[[Any], Any],
                   args: Optional[List] = None,
                   kwargs: Optional[Dict] = None) -> List[Any]:
        """Executes the provided function on all workers.

        Args:
            fn: Target function that can be executed with arbitrary
                args and keyword arguments.
            args: List of arguments to be passed into the target function.
            kwargs: Dictionary of keyword arguments to be
                passed into the target function.

        Returns:
            list: List of ObjectRefs that you can run `ray.get` on to
                retrieve values.
        """
        args = args or []
        kwargs = kwargs or {}
        return [
            worker.execute.remote(lambda w: fn(*args, **kwargs))
            for worker in self.workers
        ]

    def execute_single(self,
                       fn: Callable[["executable_cls"], Any]) -> List[Any]:
        """Executes the provided function on the rank 0 worker (chief).

        Args:
            fn: Target function to be invoked on the chief object.

        Returns:
            Deserialized return values from the target function.
        """
        return ray.get(self.workers[0].execute.remote(fn))

    def shutdown(self):
        """Destroys the provided workers."""
        for worker in self.workers:
            del worker

        if self.strategy:
            self.strategy.shutdown()
