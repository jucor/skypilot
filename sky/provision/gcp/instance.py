"""GCP instance provisioning."""
import collections
import copy
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Type

from sky import sky_logging
from sky import status_lib
from sky.adaptors import gcp
from sky.provision import common
from sky.provision.gcp import instance_utils

# Tag for user defined node types (e.g., m4xl_spot). This is used for multi
# node type clusters.
TAG_RAY_USER_NODE_TYPE = "ray-user-node-type"
# Hash of the node launch config, used to identify out-of-date nodes
TAG_RAY_LAUNCH_CONFIG = "ray-launch-config"
# Tag for autofilled node types for legacy cluster yamls without multi
# node type defined in the cluster configs.
NODE_TYPE_LEGACY_HEAD = "ray-legacy-head-node-type"
NODE_TYPE_LEGACY_WORKER = "ray-legacy-worker-node-type"

# Tag that reports the current state of the node (e.g. Updating, Up-to-date)
TAG_RAY_NODE_STATUS = "ray-node-status"

logger = sky_logging.init_logger(__name__)

MAX_POLLS = 12
# Stopping instances can take several minutes, so we increase the timeout
MAX_POLLS_STOP = MAX_POLLS * 8
POLL_INTERVAL = 5

TAG_SKYPILOT_HEAD_NODE = 'skypilot-head-node'
# Tag uniquely identifying all nodes of a cluster
TAG_RAY_CLUSTER_NAME = 'ray-cluster-name'
TAG_RAY_NODE_KIND = 'ray-node-type'

_INSTANCE_RESOURCE_NOT_FOUND_PATTERN = re.compile(
    r'The resource \'projects/.*/zones/.*/instances/.*\' was not found')


def _filter_instances(
    handlers: Iterable[Type[instance_utils.GCPInstance]],
    project_id: str,
    zone: str,
    label_filters: Dict[str, str],
    status_filters_fn: Callable[[Type[instance_utils.GCPInstance]],
                                Optional[List[str]]],
    included_instances: Optional[List[str]] = None,
    excluded_instances: Optional[List[str]] = None,
) -> Dict[Type[instance_utils.GCPInstance], List[str]]:
    """Filter instances using all instance handlers."""
    instances = set()
    logger.debug(f'handlers: {handlers}')
    for instance_handler in handlers:
        instance_dict = instance_handler.filter(
            project_id, zone, label_filters,
            status_filters_fn(instance_handler), included_instances,
            excluded_instances)
        instances |= set(instance_dict.keys())
    handler_to_instances = collections.defaultdict(list)
    for instance in instances:
        handler = instance_utils.instance_to_handler(instance)
        handler_to_instances[handler].append(instance)
    logger.debug(f'handler_to_instances: {handler_to_instances}')
    return handler_to_instances


def _wait_for_operations(
    handlers_to_operations: Dict[Type[instance_utils.GCPInstance], List[dict]],
    project_id: str,
    zone: Optional[str],
) -> None:
    """Poll for compute zone / global operation until finished.

    If zone is None, then the operation is global.
    """
    op_type = 'global' if zone is None else 'zone'
    total_polls = 0
    for handler, operations in handlers_to_operations.items():
        for operation in operations:
            logger.debug(
                f'wait_for_compute_{op_type}_operation: '
                f'Waiting for operation {operation["name"]} to finish...')
            while total_polls < MAX_POLLS:
                if handler.wait_for_operation(operation, project_id, zone):
                    break
                time.sleep(POLL_INTERVAL)
                total_polls += 1


def _get_head_instance_id(instances: List) -> Optional[str]:
    head_instance_id = None
    for inst in instances:
        labels = inst.get('labels', {})
        if (labels.get(TAG_RAY_NODE_KIND) == 'head' or
                labels.get(TAG_SKYPILOT_HEAD_NODE) == '1'):
            head_instance_id = inst['id']
            break
    return head_instance_id


def run_instances(region: str, cluster_name: str,
                  config: common.ProvisionConfig) -> common.ProvisionRecord:
    """See sky/provision/__init__.py"""
    result_dict = {}
    labels = config.tags  # gcp uses "labels" instead of aws "tags"
    labels = dict(sorted(copy.deepcopy(labels).items()))
    resumed_instance_ids: List[str] = []
    created_instance_ids: List[str] = []

    node_type = instance_utils.get_node_type(config.node_config)
    project_id = config.provider_config['project_id']
    availability_zone = config.provider_config['availability_zone']

    # SKY: "TERMINATED" for compute VM, "STOPPED" for TPU VM
    # "STOPPING" means the VM is being stopped, which needs
    # to be included to avoid creating a new VM.
    if node_type == instance_utils.GCPNodeType.COMPUTE:
        resource = instance_utils.GCPComputeInstance
        STOPPED_STATUS = 'TERMINATED'
    elif node_type == instance_utils.GCPNodeType.TPU:
        resource = instance_utils.GCPTPUVMInstance
        STOPPED_STATUS = 'STOPPED'
    else:
        raise ValueError(f'Unknown node type {node_type}')

    PENDING_STATUS = ['PROVISIONING', 'STAGING']
    filter_labels = {TAG_RAY_CLUSTER_NAME: cluster_name}

    exist_instances = resource.filter(
        project_id=project_id,
        zone=availability_zone,
        label_filters=filter_labels,
        status_filters=None,
    )
    exist_instances = list(exist_instances.values())
    head_instance_id = _get_head_instance_id(exist_instances)

    # NOTE: We are not handling REPAIRING, SUSPENDING, SUSPENDED status.
    pending_instances = []
    running_instances = []
    stopping_instances = []
    stopped_instances = []

    # SkyPilot: We try to use the instances with the same matching launch_config first. If
    # there is not enough instances with matching launch_config, we then use all the
    # instances with the same matching launch_config plus some instances with wrong
    # launch_config.
    def get_order_key(node):
        import datetime

        timestamp = node.get("lastStartTimestamp")
        if timestamp is not None:
            return datetime.datetime.strptime(timestamp,
                                              "%Y-%m-%dT%H:%M:%S.%f%z")
        return node['id']

    for inst in exist_instances:
        state = inst['status']
        if state in PENDING_STATUS:
            pending_instances.append(inst)
        elif state == 'RUNNING':
            running_instances.append(inst)
        elif state == 'STOPPING':
            stopping_instances.append(inst)
        elif state == STOPPED_STATUS:
            stopped_instances.append(inst)
        else:
            raise RuntimeError(f'Unsupported state "{state}".')

    pending_instances.sort(key=lambda n: get_order_key(n), reverse=True)
    running_instances.sort(key=lambda n: get_order_key(n), reverse=True)
    stopping_instances.sort(key=lambda n: get_order_key(n), reverse=True)
    stopped_instances.sort(key=lambda n: get_order_key(n), reverse=True)

    if head_instance_id is None:
        if running_instances:
            head_instance_id = resource.create_node_tag(
                running_instances[0]['id'])
        elif pending_instances:
            head_instance_id = resource.create_node_tag(
                pending_instances[0]['id'])

    # TODO(suquark): Maybe in the future, users could adjust the number
    #  of instances dynamically. Then this case would not be an error.
    if config.resume_stopped_nodes and len(exist_instances) > config.count:
        raise RuntimeError('The number of running/stopped/stopping '
                           f'instances combined ({len(exist_instances)}) in '
                           f'cluster "{cluster_name}" is greater than the '
                           f'number requested by the user ({config.count}). '
                           'This is likely a resource leak. '
                           'Use "sky down" to terminate the cluster.')

    # TODO: if there are running instances, use their zones instead

    to_start_count = (config.count - len(running_instances) -
                      len(pending_instances))

    # Try to reuse previously stopped nodes with compatible configs
    if config.resume_stopped_nodes and to_start_count > 0 and (
            stopping_instances or stopped_instances):
        # TODO: we should wait until stopped instances are actually stopped.
        #  However, in GCP it is hard to know whether one instance is stopping for termination.
        #  So we need to wait and check.
        stopped_nodes = stopping_instances + stopped_instances
        stopped_nodes.sort(key=lambda n: get_order_key(n), reverse=True)
        resumed_instance_ids = [n['id'] for n in stopped_nodes]
        if resumed_instance_ids:
            # TODO(suquark): Some instances could still be stopping.
            # We may wait until these instances stop.
            for node_id in resumed_instance_ids:
                result = resource.start_instance(node_id, project_id,
                                                 availability_zone)
                result_dict[node_id] = {node_id: result}
                resource.set_labels(project_id, availability_zone, node_id,
                                    labels)
        to_start_count -= len(resumed_instance_ids)

        if head_instance_id is None:
            head_instance_id = resource.create_node_tag(resumed_instance_ids[0])

    if to_start_count > 0:
        results = resource.create_instances(cluster_name, project_id,
                                            availability_zone,
                                            config.node_config, labels,
                                            to_start_count)
        # FIXME: it seems that success is always False.
        for success, instance_id in results:
            resource.set_labels(project_id, availability_zone, instance_id,
                                labels)
            created_instance_ids.append(instance_id)
        result_dict.update(
            {instance_id: result for result, instance_id in results})

        # NOTE: we only create worker tags for newly started nodes, because
        # the worker tag is a legacy feature, so we would not care about
        # more corner cases.
        if head_instance_id is None:
            head_instance_id = resource.create_node_tag(created_instance_ids[0])
            for inst in created_instance_ids[1:]:
                resource.create_node_tag(inst, is_head=False)
        else:
            for inst in created_instance_ids:
                resource.create_node_tag(inst, is_head=False)
    return common.ProvisionRecord(provider_name='gcp',
                                  region=region,
                                  zone=availability_zone,
                                  cluster_name=cluster_name,
                                  head_instance_id=head_instance_id,
                                  resumed_instance_ids=resumed_instance_ids,
                                  created_instance_ids=created_instance_ids)


def wait_instances(region: str, cluster_name: str,
                   state: Optional[status_lib.ClusterStatus]) -> None:
    """See sky/provision/__init__.py"""
    # We already wait for the instances to be running in run_instances.
    # So we don't need to wait here.
    return


def stop_instances(
    cluster_name_on_cloud: str,
    provider_config: Optional[Dict[str, Any]] = None,
    worker_only: bool = False,
) -> None:
    assert provider_config is not None, cluster_name_on_cloud
    zone = provider_config['availability_zone']
    project_id = provider_config['project_id']
    label_filters = {TAG_RAY_CLUSTER_NAME: cluster_name_on_cloud}
    if worker_only:
        label_filters[TAG_RAY_NODE_KIND] = 'worker'

    handlers: List[Type[instance_utils.GCPInstance]] = [
        instance_utils.GCPComputeInstance
    ]
    use_tpu_vms = provider_config.get('_has_tpus', False)
    if use_tpu_vms:
        handlers.append(instance_utils.GCPTPUVMInstance)

    handler_to_instances = _filter_instances(
        handlers,
        project_id,
        zone,
        label_filters,
        lambda handler: handler.NEED_TO_STOP_STATES,
    )
    all_instances = [
        i for instances in handler_to_instances.values() for i in instances
    ]

    operations = collections.defaultdict(list)
    for handler, instances in handler_to_instances.items():
        for instance in instances:
            operations[handler].append(handler.stop(project_id, zone, instance))
    _wait_for_operations(operations, project_id, zone)
    # Check if the instance is actually stopped.
    # GCP does not fully stop an instance even after
    # the stop operation is finished.
    for _ in range(MAX_POLLS_STOP):
        handler_to_instances = _filter_instances(
            handler_to_instances.keys(),
            project_id,
            zone,
            label_filters,
            lambda handler: handler.NON_STOPPED_STATES,
            included_instances=all_instances,
        )
        if not handler_to_instances:
            break
        time.sleep(POLL_INTERVAL)
    else:
        raise RuntimeError(f'Maximum number of polls: '
                           f'{MAX_POLLS_STOP} reached. '
                           f'Instance {all_instances} is still not in '
                           'STOPPED status.')


def terminate_instances(
    cluster_name_on_cloud: str,
    provider_config: Optional[Dict[str, Any]] = None,
    worker_only: bool = False,
) -> None:
    """See sky/provision/__init__.py"""
    assert provider_config is not None, cluster_name_on_cloud
    zone = provider_config['availability_zone']
    project_id = provider_config['project_id']
    use_tpu_vms = provider_config.get('_has_tpus', False)

    label_filters = {TAG_RAY_CLUSTER_NAME: cluster_name_on_cloud}
    if worker_only:
        label_filters[TAG_RAY_NODE_KIND] = 'worker'

    handlers: List[Type[instance_utils.GCPInstance]] = [
        instance_utils.GCPComputeInstance
    ]
    if use_tpu_vms:
        handlers.append(instance_utils.GCPTPUVMInstance)

    handler_to_instances = _filter_instances(handlers, project_id, zone,
                                             label_filters, lambda _: None)
    operations = collections.defaultdict(list)
    errs = []
    for handler, instances in handler_to_instances.items():
        for instance in instances:
            try:
                operations[handler].append(
                    handler.terminate(project_id, zone, instance))
            except gcp.http_error_exception() as e:
                if _INSTANCE_RESOURCE_NOT_FOUND_PATTERN.search(
                        e.reason) is None:
                    errs.append(e)
                else:
                    logger.warning(f'Instance {instance} does not exist. '
                                   'Skip terminating it.')
    _wait_for_operations(operations, project_id, zone)
    if errs:
        raise RuntimeError(f'Failed to terminate instances: {errs}')
    # We don't wait for the instances to be terminated, as it can take a long
    # time (same as what we did in ray's node_provider).


def open_ports(
    cluster_name_on_cloud: str,
    ports: List[str],
    provider_config: Optional[Dict[str, Any]] = None,
) -> None:
    """See sky/provision/__init__.py"""
    assert provider_config is not None, cluster_name_on_cloud
    zone = provider_config['availability_zone']
    project_id = provider_config['project_id']
    firewall_rule_name = provider_config['firewall_rule']

    label_filters = {TAG_RAY_CLUSTER_NAME: cluster_name_on_cloud}
    handlers: List[Type[instance_utils.GCPInstance]] = [
        instance_utils.GCPComputeInstance,
        instance_utils.GCPTPUVMInstance,
    ]
    handler_to_instances = _filter_instances(handlers, project_id, zone,
                                             label_filters, lambda _: None)
    operations = collections.defaultdict(list)
    compute_handler: Type[instance_utils.GCPInstance] = (
        instance_utils.GCPComputeInstance)
    for handler, instances in handler_to_instances.items():
        if not instances:
            logger.warning(f'No instance found for cluster '
                           f'{cluster_name_on_cloud}.')
            continue
        else:
            for instance in instances:
                # Add tags for all nodes in the cluster, so the firewall rule
                # could correctly apply to all instance in the cluster.
                handler.add_network_tag_if_not_exist(
                    project_id,
                    zone,
                    instance,
                    tag=cluster_name_on_cloud,
                )
            # If we have multiple instances, they are in the same cluster,
            # i.e. the same VPC. So we can just pick any one of them.
            vpc_name = handler.get_vpc_name(project_id, zone, instances[0])
            # Use compute handler here for both Compute VM and TPU VM,
            # as firewall rules is a compute resource.
            op = compute_handler.create_or_update_firewall_rule(
                firewall_rule_name,
                project_id,
                vpc_name,
                cluster_name_on_cloud,
                ports,
            )
            operations[compute_handler].append(op)
    # Use zone = None to indicate wait for global operations
    _wait_for_operations(operations, project_id, None)


def cleanup_ports(
    cluster_name_on_cloud: str,
    provider_config: Optional[Dict[str, Any]] = None,
) -> None:
    """See sky/provision/__init__.py"""
    assert provider_config is not None, cluster_name_on_cloud
    project_id = provider_config['project_id']
    if 'ports' in provider_config:
        # Backward compatibility for old provider config.
        # TODO(tian): remove this after 2 minor releases, 0.6.0.
        for port in provider_config['ports']:
            firewall_rule_name = f'user-ports-{cluster_name_on_cloud}-{port}'
            instance_utils.GCPComputeInstance.delete_firewall_rule(
                project_id, firewall_rule_name)
    if 'firewall_rule' in provider_config:
        firewall_rule_name = provider_config['firewall_rule']
        instance_utils.GCPComputeInstance.delete_firewall_rule(
            project_id, firewall_rule_name)
