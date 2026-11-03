"""Reject stale/unbound local contact observations; no live process operations."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch
from test_r4_observation_refresh import load

runtime, _ = load('candidate')


class ContactObservation(unittest.TestCase):
    def setUp(self):
        self.state = Path('/profile')
        self.config = {'role': 'node', 'node_id': 'node-1'}
        self.children = [{'role':'node','pid':123,'birth':'456','alive':True}]
        self.row = {'schema':1,'node_id':'node-1','worker_pid':123,'worker_birth':'456',
                    'last_broker_acceptance_unix':99,
                    'last_accepted_operation':'node.heartbeat','heartbeat_v1':True}

    def view(self):
        with patch.object(runtime,'_read',lambda *a:self.row), \
                patch.object(runtime.time,'time',lambda:100):
            return runtime._node_connection(self.state,self.config,self.children)

    def test_current_process_fresh_contact_is_visible(self):
        result=self.view()
        self.assertTrue(result['broker_acceptance_fresh'])
        self.assertEqual(result['last_broker_acceptance_unix'],99)
        self.assertTrue(result['heartbeat_v1'])

    def test_other_node_other_pid_reused_pid_birth_and_boolean_pid_rejected(self):
        for field,value in [('node_id','other-node'),('worker_pid',124),
                            ('worker_pid',True),('worker_birth','old-birth'),('schema',True)]:
            with self.subTest(field=field,value=value):
                old=copy.deepcopy(self.row);self.row[field]=value
                self.assertFalse(self.view()['broker_acceptance_fresh'])
                self.assertIsNone(self.view()['last_broker_acceptance_unix'])
                self.row=old

    def test_dead_or_ambiguous_worker_does_not_claim_accepted_connection(self):
        for children in [[],[dict(self.children[0],alive=False)],self.children*2]:
            self.children=children
            self.assertFalse(self.view()['broker_acceptance_fresh'])

    def test_missing_malformed_and_untrusted_contact_are_unavailable(self):
        for row in [{},[],None,'instructions',{'schema':1}]:
            self.row=row
            self.assertFalse(self.view()['broker_acceptance_fresh'])

    def test_old_future_nonnumeric_boolean_nonfinite_and_huge_stamps_rejected(self):
        for stamp in [84,101,True,None,'99',float('inf'),float('-inf'),float('nan'),10**400]:
            with self.subTest(stamp=str(stamp)[:20]):
                self.row['last_broker_acceptance_unix']=stamp
                self.assertFalse(self.view()['broker_acceptance_fresh'])

    def test_unknown_operation_is_not_rendered_as_protocol_evidence(self):
        for value in [[],{},'exec arbitrary instructions']:
            self.row['last_accepted_operation']=value
            self.assertIsNone(self.view()['last_accepted_operation'])

    def test_central_has_no_node_contact_observation(self):
        self.config['role']='central'
        self.assertIsNone(self.view())


if __name__=='__main__': unittest.main(verbosity=2)
