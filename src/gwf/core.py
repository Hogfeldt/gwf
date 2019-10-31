import functools
import logging
import os
import os.path
from collections import defaultdict
from enum import Enum

from .exceptions import WorkflowError
from .models import get_target_meta, open_db
from .utils import LazyDict, cache, load_workflow, parse_path, timer

logger = logging.getLogger(__name__)


def workflow_from_path(path):
    """Return workflow object for the workflow given by `path`.

    Returns a :class:`~gwf.Workflow` object containing the workflow object of
    the workflow given by `path`.

    :arg str path:
        Path to a workflow file, optionally specifying a workflow object in that
        file.
    """
    basedir, filename, obj = parse_path(path)
    return load_workflow(basedir, filename, obj)


def workflow_from_config(config):
    """Return workflow object for the workflow specified by `config`.

    See :func:`workflow_from_path` for further information.
    """
    return workflow_from_path(config["file"])


def graph_from_path(path):
    """Return graph for the workflow given by `path`.

    Returns a :class:`~gwf.Graph` object containing the workflow graph of the
    workflow given by `path`. Note that calling this function computes the
    complete dependency graph which may take some time for large workflows.

    :arg str path:
        Path to a workflow file, optionally specifying a workflow object in that
        file.
    """
    workflow = workflow_from_path(path)
    return Graph.from_targets(workflow.targets)


def graph_from_config(config):
    """Return graph for the workflow specified by `config`.

    See :func:`graph_from_path` for further information.
    """
    return graph_from_path(config["file"])


class TargetStatus(Enum):
    """Status of a target, as reported by the scheduler."""

    SHOULDRUN = 0  #: The target should run.
    SUBMITTED = 1  #: The target has been submitted, but is not currently running.
    RUNNING = 2  #: The target is currently running.
    COMPLETED = 3  #: The target has completed and should not run.
    FAILED = 4
    KILLED = 5
    CANCELLED = 6


class Graph:
    """Represents a dependency graph for a set of targets.

    The graph represents the targets present in a workflow, but also their
    dependencies and the files they provide.

    During construction of the graph the dependencies between targets are
    determined by looking at target inputs and outputs. If a target specifies a
    file as input, the file must either be provided by another target or
    already exist on disk. In case that the file is provided by another target,
    a dependency to that target will be added:

    :ivar dict dependencies:
        A dictionary mapping a target to a set of its dependencies.

    If the file is not provided by another target, the file is *unresolved*:

    :ivar set unresolved:
        A set containing file paths of all unresolved files.

    If the graph is constructed successfully, the following instance variables
    will be available:

    :ivar dict targets:
        A dictionary mapping target names to instances of :class:`gwf.Target`.
    :ivar dict provides:
        A dictionary mapping a file path to the target that provides that path.
    :ivar dict dependents:
        A dictionary mapping a target to a set of all targets which depend on
        the target.

    The graph can be manipulated in arbitrary, diabolic ways after it has been
    constructed. Checks are only performed at construction-time, thus
    introducing e.g. a circular dependency by manipulating *dependencies* will
    not raise an exception.

    :raises gwf.exceptions.WorkflowError:
        Raised if the workflow contains a circular dependency.
    """

    def __init__(self, targets, provides, dependencies, dependents, unresolved):
        self.targets = targets
        self.provides = provides
        self.dependencies = dependencies
        self.dependents = dependents
        self.unresolved = unresolved

        self._check_for_circular_dependencies()

    @classmethod
    def from_targets(cls, targets):
        """Construct a dependency graph from a set of targets.

        When a graph is initialized it computes all dependency relations
        between targets, ensuring that the graph is semantically sane.
        Therefore, construction of the graph is an expensive operation which
        may raise a number of exceptions:

        :raises gwf.exceptions.FileProvidedByMultipleTargetsError:
            Raised if the same file is provided by multiple targets.

        Since this method initializes the graph, it may also raise:

        :raises gwf.exceptions.WorkflowError:
            Raised if the workflow contains a circular dependency.
        """
        provides = {}
        unresolved = set()
        dependencies = defaultdict(set)
        dependents = defaultdict(set)

        logger.debug("Building dependency graph from %d targets", len(targets))

        with timer("Built dependency graph in %.3fms", logger=logger):
            for target in targets.values():
                for path in target.flattened_outputs():
                    if path in provides:
                        msg = 'File "{}" provided by targets "{}" and "{}".'.format(
                            path, provides[path].name, target
                        )
                        raise WorkflowError(msg)
                    provides[path] = target

        for target in targets.values():
            for path in target.flattened_inputs():
                if path in provides:
                    dependencies[target].add(provides[path])
                else:
                    unresolved.add(path)

        for target, deps in dependencies.items():
            for dep in deps:
                dependents[dep].add(target)

        return cls(
            targets=targets,
            provides=provides,
            dependencies=dependencies,
            dependents=dependents,
            unresolved=unresolved,
        )

    @timer("Checked for circular dependencies in %.3fms", logger=logger)
    def _check_for_circular_dependencies(self):
        """Check for circular dependencies in the graph.

        Raises :class:`WorkflowError` if a circular dependency is found.
        """
        logger.debug("Checking for circular dependencies")

        fresh, started, done = 0, 1, 2

        nodes = self.targets.values()
        state = dict((n, fresh) for n in nodes)

        def visitor(node):
            state[node] = started
            for dep in self.dependencies[node]:
                if state[dep] == started:
                    raise WorkflowError("Target {} depends on itself.".format(node))
                elif state[dep] == fresh:
                    visitor(dep)
            state[node] = done

        for node in nodes:
            if state[node] == fresh:
                visitor(node)

    def endpoints(self):
        """Return a set of all targets that are not depended on by other targets."""
        return set(self.targets.values()) - set(self.dependents.keys())

    @cache
    def dfs(self, root):
        """Return the depth-first traversal path through a graph from `root`."""
        visited = set()
        path = []

        def dfs_inner(node):
            if node in visited:
                return

            visited.add(node)
            for dep in self.dependencies[node]:
                dfs_inner(dep)
            path.append(node)

        dfs_inner(root)
        return path

    def __iter__(self):
        return iter(self.targets.values())

    def __len__(self):
        return len(self.targets)

    def __getitem__(self, target_name):
        return self.targets[target_name]

    def __contains__(self, target_name):
        return target_name in self.targets


def _fileinfo(path):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    else:
        return st.st_mtime


FileCache = functools.partial(LazyDict, valfunc=_fileinfo)


class Scheduler:
    """Schedule one or more targets and submit to a backend.

    Scheduling a target will determine whether the target needs to run based on
    whether it already has been submitted and whether any of its dependencies
    have been submitted.

    Targets that should run will be submitted to *backend*, unless *dry_run* is
    set to ``True``.

    When scheduling a target, the scheduler checks whether any of its inputs
    are unresolved, meaning that during construction of the graph, no other
    target providing the file was found. This means that the file should then
    exist on disk. If it doesn't the following exception is raised:

    :raises gwf.exceptions.FileRequiredButNotProvidedError:
        Raised if a target has an input file that does not exist on the file
        system and that is not provided by another target.
    """

    SHOULDRUN_STATES = (
        TargetStatus.FAILED,
        TargetStatus.KILLED,
        TargetStatus.CANCELLED,
        TargetStatus.SHOULDRUN,
    )

    def __init__(
        self, graph, backend, dry_run=False, file_cache=FileCache(), state_db=None
    ):
        """
        :param gwf.Graph graph:
            Graph of the workflow.
        :param gwf.backends.Backend backend:
            An instance of :class:`gwf.backends.Backend` to which targets will
            be submitted.
        :param bool dry_run:
            If ``True``, targets will not be submitted to the backend. Defaults
            to ``False``.
        """
        self.graph = graph
        self.backend = backend
        self.dry_run = dry_run

        self._file_cache = file_cache
        self._pretend_known = set()
        self._state_db = state_db or open_db()

    def prepare_target_options(self, target):
        """Apply backend-specific option defaults to a target.

        Injects backend target defaults into the target options and checks
        whether the option in the given target are supported by the backend.
        Warns the user and removes the option if this is not the case.
        """
        new_options = dict(self.backend.option_defaults)
        new_options.update(target.options)

        for option_name, option_value in list(new_options.items()):
            if option_name not in self.backend.option_defaults.keys():
                logger.warning(
                    'Option "{}" used in "{}" is not supported by backend. Ignored.'.format(
                        option_name, target.name
                    )
                )
                del new_options[option_name]
            elif option_value is None:
                del new_options[option_name]
        target.options = new_options

    def schedule(self, target):
        """Schedule a target and its dependencies.

        Returns ``True`` if *target* was submitted to the backend (even when
        *dry_run* is ``True``).

        :param gwf.Target target:
            Target to be scheduled.
        """
        logger.debug("Scheduling target %s", target)
        self.prepare_target_options(target)

        if (
            self.status(target) == TargetStatus.SUBMITTED
            or target in self._pretend_known
        ):
            logger.debug("Target %s has already been submitted", target)
            return True

        submitted_deps = set()
        for dependency in sorted(self.graph.dependencies[target], key=lambda t: t.name):
            if self.schedule(dependency):
                submitted_deps.add(dependency)

        if submitted_deps or self.status(target) in Scheduler.SHOULDRUN_STATES:
            if self.dry_run:
                logger.info("Would submit target %s", target)
                self._pretend_known.add(target)
            else:
                logger.info("Submitting target %s", target)

                state = get_target_meta(target, db=self._state_db)
                state.reset(autocommit=False)
                state.submitted(autocommit=False)
                state.commit()

                self.backend.submit(target, dependencies=submitted_deps)
            return True
        else:
            logger.debug("Target %s should not run", target)
            return False

    def schedule_many(self, targets):
        """Schedule multiple targets and their dependencies.

        This is a convenience method for scheduling multiple targets. See
        :func:`schedule` for a detailed description of the arguments and
        behavior.

        :param list targets:
            A list of targets to be scheduled.
        """
        logger.debug("Scheduling %d targets", len(targets))

        schedules = []
        submitted_targets = 0
        with timer("Scheduled targets in %.3fms", logger=logger):
            for target in targets:
                was_submitted = self.schedule(target)
                if was_submitted:
                    submitted_targets += 1
                schedules.append(was_submitted)
        logger.debug("Submitted %d targets", submitted_targets)
        return schedules

    def update_state(self, target):
        logger.debug("Updating state of %s", target)
        state = get_target_meta(target, self._state_db)

        for dep in self.graph.dependencies[target]:
            dep_state = self.update_state(dep)
            if (
                dep_state.is_failed()
                or dep_state.is_killed()
                or dep_state.is_cancelled()
                or dep_state.is_unknown()
            ):
                state.reset()
        return state

    @cache
    def should_run(self, target):
        """Return whether a target should be run or not."""

        for dep in self.graph.dependencies[target]:
            if self.should_run(dep):
                logger.debug(
                    "%s should run because its dependency %s should run", target, dep
                )
                return True

        # Check whether all input files actually exists are are being provided
        # by another target. If not, it's an error.
        for path in target.flattened_inputs():
            if path in self.graph.unresolved and self._file_cache[path] is None:
                msg = (
                    'File "{}" is required by "{}", but does not exist and is not '
                    "provided by any target in the workflow."
                ).format(path, target)
                raise WorkflowError(msg)

        if target.is_sink:
            logger.debug("%s should run because it is a sink", target)
            return True

        for path in target.flattened_outputs():
            if self._file_cache[path] is None:
                logger.debug(
                    "%s should run because its output file %s does not exist",
                    target,
                    path,
                )
                return True

        if target.is_source:
            logger.debug("%s should not run because it is a source", target)
            return False

        youngest_in_ts, youngest_in_path = max(
            (self._file_cache[path], path) for path in target.flattened_inputs()
        )
        logger.debug(
            "%s is the youngest input file of %s with timestamp %s",
            youngest_in_path,
            target,
            youngest_in_ts,
        )

        oldest_out_ts, oldest_out_path = min(
            (self._file_cache[path], path) for path in target.flattened_outputs()
        )
        logger.debug(
            "%s is the oldest output file of %s with timestamp %s",
            oldest_out_path,
            target,
            youngest_in_ts,
        )

        if youngest_in_ts > oldest_out_ts:
            logger.debug(
                "%s should run because input file %s is newer than output file %s",
                target,
                youngest_in_path,
                oldest_out_path,
            )
            return True
        return False

    def status(self, target):
        """Return the status of a target.

        Returns the status of a target where it is taken into account whether
        the target should run or not.

        :param Target target:
            The target to return status for.
        """

        state = self.update_state(target)
        should_run = self.should_run(target)
        if state.is_unknown():
            if should_run:
                return TargetStatus.SHOULDRUN
            else:
                return TargetStatus.COMPLETED
        elif state.is_submitted():
            return TargetStatus.SUBMITTED
        elif state.is_running():
            return TargetStatus.RUNNING
        elif state.is_completed():
            if should_run:
                return TargetStatus.SHOULDRUN
            else:
                return TargetStatus.COMPLETED
        elif state.is_failed():
            return TargetStatus.FAILED
        elif state.is_cancelled():
            return TargetStatus.CANCELLED
        elif state.is_killed():
            return TargetStatus.KILLED
