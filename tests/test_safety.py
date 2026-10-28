import importlib.util
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS

import oci

spec = importlib.util.spec_from_file_location('try_create', 'scripts/try_create.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SafetyTest(unittest.TestCase):
    @patch.dict(os.environ, {'OCI_COMPARTMENT_ID':'root','AD_NAME':'ad'})
    def test_either_free_shape_stops_other_shape_from_creating_a_second_vm(self):
        identity=NS(
            list_region_subscriptions=lambda _: NS(data=[NS(is_home_region=True,region_name='us-sanjose-1')]),
            list_availability_domains=lambda _: NS(data=[NS(name='ad')]),
            list_compartments=object())
        compute=NS(list_instances=object())
        for target,existing in [(module.ARM,module.AMD),(module.AMD,module.ARM)]:
            server=NS(shape=existing,lifecycle_state='RUNNING',freeform_tags={})
            with patch.object(module,'SHAPE',target), \
                 patch.object(module,'load_config',return_value={'tenancy':'root','region':'us-sanjose-1'}), \
                 patch.object(module,'clients',return_value=(identity,compute,NS(),NS())), \
                 patch.object(module,'all_items',side_effect=[[],[server]]) as listing, \
                 patch.object(module,'output') as output:
                module.check()
                output.assert_called_once_with(status='existing')
                self.assertEqual(listing.call_count,2)

    def history(self, status='completed'):
        return [{'id': 1, 'status': status, 'created_at': '2026-10-24T00:00:00Z'}]

    def safe(self, steps, status='completed'):
        return module.previous_run_safe(self.history(status), lambda _: [{'steps': steps}], 2)

    def test_first_run(self):
        self.assertTrue(module.previous_run_safe([], lambda _: [], 2))

    def test_capacity_can_retry(self):
        self.assertTrue(self.safe([{'name': module.REQUEST_STEP, 'conclusion': 'success'},
                                  {'name': module.RETRY_MARKER, 'conclusion': 'success'}]))

    def test_accepted_cannot_retry(self):
        self.assertFalse(self.safe([{'name': module.REQUEST_STEP, 'conclusion': 'success'}]))

    def test_crashed_or_cancelled_cannot_retry(self):
        for conclusion in ['failure', 'cancelled', None]:
            self.assertFalse(self.safe([{'name': module.REQUEST_STEP, 'conclusion': conclusion}]))

    def test_failed_preflight_can_retry(self):
        self.assertTrue(self.safe([{'name': module.REQUEST_STEP, 'conclusion': 'skipped'}]))

    def test_overlapping_run_blocks(self):
        self.assertFalse(self.safe([], 'in_progress'))

    def test_run_start_order_not_run_number(self):
        runs = self.history() + [{'id': 5, 'status': 'completed', 'created_at': '2026-10-24T01:00:00Z'}]
        self.assertFalse(module.previous_run_safe(runs, lambda _: [{'steps':[{'name':module.REQUEST_STEP,'conclusion':'failure'}]}], 2))

    @patch.object(module, 'SHAPE', 'VM.Standard.A1.Flex')
    @patch.dict(os.environ, {'OCI_COMPARTMENT_ID':'root', 'AD_NAME':'ad', 'IMAGE_ID':'image',
                            'OCI_SUBNET_ID':'subnet', 'SSH_PUBLIC_KEY':'ssh-rsa public',
                            'GITHUB_REPOSITORY':'owner/repo'})
    def test_launch_is_bounded_and_not_retried(self):
        recorded = []
        def send(details, **kwargs):
            recorded.append((details, kwargs))
            return NS(data=NS(lifecycle_state='PROVISIONING'))
        with patch.object(module,'load_config',return_value={}), \
             patch.object(module.oci.core,'ComputeClient',return_value=NS(launch_instance=send)), \
             patch.object(module,'output') as output:
            module.launch()
        self.assertEqual(len(recorded), 1)
        details, kwargs = recorded[0]
        self.assertEqual(details.shape, 'VM.Standard.A1.Flex')
        self.assertEqual((details.shape_config.ocpus, details.shape_config.memory_in_gbs),(2,12))
        self.assertEqual(details.source_details.boot_volume_size_in_gbs,100)
        self.assertEqual(details.source_details.boot_volume_vpus_per_gb,10)
        self.assertIsInstance(kwargs['retry_strategy'],oci.retry.NoneRetryStrategy)
        self.assertEqual(len(kwargs['opc_retry_token']),64)
        output.assert_called_once_with(status='accepted')

    @patch.dict(os.environ, {'OCI_COMPARTMENT_ID':'root', 'AD_NAME':'ad', 'IMAGE_ID':'image',
                            'OCI_SUBNET_ID':'subnet', 'SSH_PUBLIC_KEY':'ssh-rsa public',
                            'GITHUB_REPOSITORY':'owner/repo'})
    def test_only_explicit_capacity_allows_retry(self):
        for error, expected in [(oci.exceptions.ServiceError(500,'InternalError',{},'Out of host capacity.'),'capacity'),
                                (oci.exceptions.ServiceError(401,'NotAuthenticated',{},'Authentication failed'),'uncertain'),
                                (TimeoutError(),'uncertain')]:
            with patch.object(module,'load_config',return_value={}), \
                 patch.object(module.oci.core,'ComputeClient') as client, \
                 patch.object(module,'output') as output:
                client.return_value.launch_instance.side_effect=error
                module.launch()
                output.assert_called_once_with(status=expected)
                self.assertEqual(client.return_value.launch_instance.call_count,1)

    @patch.object(module, 'SHAPE', 'VM.Standard.E2.1.Micro')
    @patch.dict(os.environ, {'OCI_COMPARTMENT_ID':'root', 'AD_NAME':'ad', 'IMAGE_ID':'arm-image',
                            'OCI_SUBNET_ID':'subnet', 'SSH_PUBLIC_KEY':'ssh-rsa public',
                            'GITHUB_REPOSITORY':'owner/repo'})
    def test_amd_uses_fixed_free_shape_and_x86_platform_image(self):
        with patch.object(module,'load_config',return_value={}), \
             patch.object(module.oci.core,'ComputeClient') as client, \
             patch.object(module,'output') as output:
            client.return_value.launch_instance.return_value=NS(data=NS(lifecycle_state='PROVISIONING'))
            module.launch()
            self.assertEqual(client.return_value.launch_instance.call_count,1)
            details=client.return_value.launch_instance.call_args.args[0]
            self.assertEqual(details.shape,'VM.Standard.E2.1.Micro')
            self.assertIsNone(details.shape_config)
            self.assertEqual(details.source_details.image_id,module.AMD_PLATFORM_IMAGE)
            self.assertNotEqual(details.source_details.image_id,'arm-image')
            self.assertEqual(details.source_details.boot_volume_size_in_gbs,50)
            self.assertEqual(details.source_details.boot_volume_vpus_per_gb,10)
            output.assert_called_once_with(status='accepted')


if __name__ == '__main__':
    unittest.main()
