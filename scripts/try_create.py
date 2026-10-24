"""One bounded OCI launch attempt; fail closed after an ambiguous previous run."""
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request

import oci

SHAPE = 'VM.Standard.A1.Flex'
NAME = 'oci-free-arm'
MARKER = 'oci-free-arm-instance'
RETRY_MARKER = 'Safe to retry: capacity unavailable'
REQUEST_STEP = 'Request one instance'


def output(**values):
    target = os.environ.get('GITHUB_OUTPUT')
    if target:
        with open(target, 'a') as f:
            for key, value in values.items():
                f.write(f'{key}={value}\n')


def summary(message):
    print(message)
    target = os.environ.get('GITHUB_STEP_SUMMARY')
    if target:
        with open(target, 'a') as f:
            f.write(message + '\n\n')


def github(path):
    req = urllib.request.Request(
        'https://api.github.com/' + path,
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28'},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def previous_run_safe(runs, jobs, current_run):
    previous = [r for r in runs if str(r['id']) != str(current_run)
                and r['status'] not in ('queued', 'waiting', 'pending', 'requested')]
    if not previous:
        return True
    latest = max(previous, key=lambda r: r.get('run_started_at') or r['created_at'])
    if latest['status'] != 'completed':
        return False
    steps = [s for job in jobs(latest['id']) for s in job.get('steps', [])]
    requests = [s for s in steps if s['name'] == REQUEST_STEP]
    if not requests or all(s.get('conclusion') == 'skipped' for s in requests):
        return True
    return any(s['name'] == RETRY_MARKER and s.get('conclusion') == 'success'
               for s in steps)


def load_config():
    return {'user': os.environ['OCI_CLI_USER'],
            'tenancy': os.environ['OCI_CLI_TENANCY'],
            'region': os.environ['OCI_CLI_REGION'],
            'fingerprint': os.environ['OCI_CLI_FINGERPRINT'],
            'key_content': os.environ['OCI_CLI_KEY_CONTENT']}


def clients(config):
    options = {'retry_strategy': oci.retry.NoneRetryStrategy(), 'timeout': (15, 60)}
    return (oci.identity.IdentityClient(config, **options),
            oci.core.ComputeClient(config, **options),
            oci.core.VirtualNetworkClient(config, **options),
            oci.core.BlockstorageClient(config, **options))


def all_items(method, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(method, *args, **kwargs).data


def check():
    config = load_config()
    identity, compute, network, block = clients(config)
    root = config['tenancy']
    if config['region'] != 'us-sanjose-1' or os.environ['OCI_COMPARTMENT_ID'] != root:
        raise ValueError('Unexpected region or compartment; no launch attempted.')
    subscriptions = identity.list_region_subscriptions(root).data
    if not any(r.is_home_region and r.region_name == config['region'] for r in subscriptions):
        raise ValueError('Target is not the home region; no launch attempted.')
    compartments = [root] + [c.id for c in all_items(
        identity.list_compartments, root, compartment_id_in_subtree=True,
        access_level='ACCESSIBLE') if c.lifecycle_state == 'ACTIVE']
    ads = identity.list_availability_domains(root).data
    if os.environ['AD_NAME'] not in [a.name for a in ads]:
        raise ValueError('Availability domain does not match account.')
    used_gb = 0
    for compartment in compartments:
        instances = all_items(compute.list_instances, compartment)
        arm = [i for i in instances if i.shape == SHAPE and i.lifecycle_state != 'TERMINATED']
        if arm:
            managed = [i for i in arm if i.freeform_tags.get('managed-by') == MARKER]
            summary('EXISTING: A1 instance already exists. No new instance will be created.'
                    + (' This workflow created it.' if managed else ' Review it in the OCI console.'))
            output(status='existing')
            return
        for ad in ads:
            used_gb += sum(v.size_in_gbs for v in all_items(
                block.list_boot_volumes, availability_domain=ad.name, compartment_id=compartment)
                if v.lifecycle_state != 'TERMINATED')
        used_gb += sum(v.size_in_gbs for v in all_items(
            block.list_volumes, compartment_id=compartment)
            if v.lifecycle_state != 'TERMINATED')
    if used_gb + 100 > 200:
        raise ValueError('Adding 100 GB would exceed the 200 GB free storage allowance.')
    subnet = network.get_subnet(os.environ['OCI_SUBNET_ID']).data
    if subnet.prohibit_public_ip_on_vnic or subnet.lifecycle_state != 'AVAILABLE':
        raise ValueError('Subnet is not an available public subnet.')
    image = compute.get_image(os.environ['IMAGE_ID']).data
    if image.operating_system != 'Canonical Ubuntu' or image.operating_system_version != '24.04':
        raise ValueError('Only Canonical Ubuntu 24.04 is allowed.')
    if image.compartment_id or image.launch_mode == 'EMULATED':
        raise ValueError('Only an Oracle platform image is allowed.')
    compatible = all_items(compute.list_image_shape_compatibility_entries, image.id)
    if SHAPE not in [entry.shape for entry in compatible]:
        raise ValueError('Image is not compatible with the A1 ARM shape.')
    repo = os.environ.get('GITHUB_REPOSITORY')
    if repo:
        runs = github(f'repos/{repo}/actions/workflows/create-vm.yml/runs?per_page=100')['workflow_runs']
        def jobs(run_id):
            return github(f'repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100')['jobs']
        if not previous_run_safe(runs, jobs, os.environ['GITHUB_RUN_ID']):
            summary('BLOCKED: A previous request may have been accepted. Stopping for manual review; no new request sent.')
            output(status='blocked')
            return
    summary('Preflight passed: home region, no existing A1, public subnet, Ubuntu ARM image, storage within 200 GB. Target: 2 OCPU / 12 GB RAM / 100 GB boot volume.')
    output(status='ready')


def launch():
    config = load_config()
    compute = oci.core.ComputeClient(config, retry_strategy=oci.retry.NoneRetryStrategy(), timeout=(15, 90))
    # Stable per repository. Never use automatic launch retries after uncertain transport errors.
    token = hashlib.sha256(('oci-free-arm-v1:' + os.environ['GITHUB_REPOSITORY']).encode()).hexdigest()
    details = oci.core.models.LaunchInstanceDetails(
        compartment_id=os.environ['OCI_COMPARTMENT_ID'],
        availability_domain=os.environ['AD_NAME'],
        display_name=NAME, shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=2, memory_in_gbs=12),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=os.environ['IMAGE_ID'], boot_volume_size_in_gbs=100, boot_volume_vpus_per_gb=10),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=os.environ['OCI_SUBNET_ID'], assign_public_ip=True),
        metadata={'ssh_authorized_keys': os.environ['SSH_PUBLIC_KEY']},
        freeform_tags={'managed-by': MARKER},
        is_pv_encryption_in_transit_enabled=True,
    )
    try:
        response = compute.launch_instance(details, opc_retry_token=token,
                                           retry_strategy=oci.retry.NoneRetryStrategy())
    except oci.exceptions.ServiceError as error:
        if error.status == 500 and 'out of host capacity' in error.message.lower():
            summary('CAPACITY_UNAVAILABLE: Oracle returned Out of host capacity. No instance accepted. The next scheduled run may retry.')
            output(status='capacity')
        else:
            summary(f'STOPPED: launch returned HTTP {error.status} ({error.code}). Review before any further request. No automatic retry.')
            output(status='uncertain')
        return
    except Exception as error:
        summary(f'UNCERTAIN: {type(error).__name__} while submitting launch. Stopping to prevent a duplicate. Check OCI before resuming.')
        output(status='uncertain')
        return
    if response.data.lifecycle_state in ('PROVISIONING', 'STARTING', 'RUNNING'):
        summary('ACCEPTED: Oracle accepted the instance. Automated attempts will stop now. Check OCI for RUNNING and its public IP; SSH connectivity has not yet been verified.')
        output(status='accepted')
    else:
        summary('UNCERTAIN: unexpected launch response. Stop and check OCI before resuming.')
        output(status='uncertain')


if __name__ == '__main__':
    try:
        {'check': check, 'launch': launch}[sys.argv[1]]()
    except Exception as error:
        # Avoid printing SDK exception bodies containing account/resource identifiers.
        summary(f'PRECHECK_FAILED: {type(error).__name__}. No launch request from this step. Check configuration and access.')
        raise SystemExit(1)
