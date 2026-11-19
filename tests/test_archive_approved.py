import unittest
import json
import re
from unittest.mock import patch
import test_monitor as base
import app
import archive_approved as m

class ApprovedMoveTests(unittest.TestCase):
    def setUp(self):
        self.fixture=base.MonitorTests(); self.fixture.setUp()
        self.sp={'sheetId':1,'gridProperties':{'columnCount':8,'rowCount':100}}
        self.dp={'sheetId':2,'gridProperties':{'columnCount':5,'rowCount':3}}
    def test_exact_scope(self):
        self.assertEqual(len(m.APPROVED),69)
        self.assertEqual(len(set(m.APPROVED.values())),69)
        self.assertTrue(all(re.fullmatch(r'0x[0-9a-f]{40}',a) for a in m.APPROVED.values()))
        self.assertFalse(set(m.APPROVED)&{344,345,253,307,338,422})
    def test_matching_is_address_based_and_preserves_secret_cell(self):
        a=m.APPROVED[380]
        rows=[['email','address','seed'],['test',a,'SECRET'],[],['other','0x'+'b'*40]]
        self.assertEqual(m.plan_rows(rows,[['email','address','seed']],{380:a}),[1])
        self.assertEqual(rows[1][2],'SECRET')
    def test_refuses_missing_duplicates_and_existing_archive(self):
        a=m.APPROVED[380];row=['test',a,'SECRET'];header=['email','address']
        for src,dst in [([header],[header]),([header,row,row],[header]),([header,row],[header,row])]:
            with self.assertRaises(app.RemoteError):m.plan_rows(src,dst,{380:a})
    def test_formulas_freeze_but_seed_and_address_are_preserved(self):
        a=m.APPROVED[380]
        src=[['header'],['test',a,'SECRET','=SUM(D3:D9)','=A1','']]
        values=[['header'],['test',a,'SECRET',123,'=literal','']]
        self.assertEqual(m.plan_rows(src,[['header']],{380:a}),[1])
        self.assertEqual(m.frozen_rows(src,values,[1]),[['test',a,'SECRET',123,'=literal']])
        self.assertEqual(m.cell_value('=literal'),{'userEnteredValue':{'stringValue':'=literal'}})
        self.assertEqual(m.cell_value(False),{'userEnteredValue':{'boolValue':False}})
        self.assertEqual(m.cell_value(0),{'userEnteredValue':{'numberValue':0}})

    def test_changed_effective_address_is_rejected(self):
        with self.assertRaises(app.RemoteError):
            m.frozen_rows([['a',m.APPROVED[380]]],[['a',m.APPROVED[381]]],[0])
    def test_atomic_plan_backs_up_copies_all_columns_then_deletes_descending(self):
        requests=m.requests_for(self.sp,self.dp,[1,4],3,'abc',[['one'],['two']])
        self.assertIn('duplicateSheet',requests[0]); self.assertIn('duplicateSheet',requests[1])
        copies=[r['copyPaste'] for r in requests if 'copyPaste' in r]
        self.assertEqual([r['destination']['startRowIndex'] for r in copies],[3,4])
        self.assertEqual([r['source']['endColumnIndex'] for r in copies],[8,8])
        self.assertFalse(any('deleteDimension' in r for r in requests))
        clears=[r['updateCells'] for r in requests if 'updateCells' in r and r['updateCells']['range']['sheetId']==1]
        self.assertEqual([r['range']['startRowIndex'] for r in clears],[1,4])
        self.assertTrue(all(r['rows']==[] and r['fields']=='userEnteredValue' for r in clears))
        writes=[r['updateCells'] for r in requests if 'updateCells' in r and r['updateCells']['range']['sheetId']==2]
        self.assertEqual(writes[0]['rows'][0]['values'],[{'userEnteredValue':{'stringValue':'one'}}])
        self.assertEqual(requests[-1]['createDeveloperMetadata']['developerMetadata']['metadataValue'],'abc')
    def test_no_metadata_after_timeout_never_resends(self):
        from unittest.mock import Mock
        api=Mock();api.metadata.return_value={}
        with self.assertRaises(app.RemoteError):m.reconcile(api,{'job':'abc'})
        api.request.assert_not_called()
    def test_bad_readback_never_deletes_local_wallet(self):
        from unittest.mock import Mock
        api=Mock();api.metadata.return_value={'sheets':[{'properties':dict(self.sp,title=m.SOURCE)},{'properties':dict(self.dp,title=m.DEST)}],
            'developerMetadata':[{'metadataKey':'bnb_archive_approved','metadataValue':'abc'}]}
        api.rows.return_value=[['unexpected']]
        j={'job':'abc','source_after_hash':'wrong','archive_after_hash':'wrong'}
        with patch.object(m,'finish_local') as finish:
            with self.assertRaises(app.RemoteError):m.reconcile(api,j)
            finish.assert_not_called()
        api.request.assert_not_called()
    def test_local_cleanup_is_idempotent_and_keeps_history(self):
        self.fixture.wallet()
        with app.db() as c:
            row=dict(c.execute('SELECT * FROM wallets').fetchone())
        j={'job':'abc','selected':{str(row['id']):row['address']}}
        with patch.object(m,'save'):
            m.finish_local(j);m.finish_local(j)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM wallets').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM archived_wallet_records').fetchone()[0],1)
            saved=json.loads(c.execute('SELECT data FROM archived_wallet_records').fetchone()[0])
            self.assertEqual(saved,row)
    def test_sheet_import_uses_defined_read_timeout(self):
        import sheets_sync
        from unittest.mock import MagicMock, Mock
        response=MagicMock()
        response.__enter__.return_value.read.return_value=b'{"range":"test!A1:B2","values":[["email","address"]]}'
        with patch.object(sheets_sync,'google_credentials',return_value=Mock(token='test')), patch.object(sheets_sync,'urlopen',return_value=response) as opened:
            self.assertEqual(sheets_sync.fetch_rows({'sheets_id':'test','sheets_tab':'test'}),[['email','address']])
            self.assertEqual(opened.call_args.kwargs['timeout'],20)

    def test_normalization_only_trims_trailing_empty_cells(self):
        self.assertEqual(m.normalize([['a','','b',''],[],[]]),[['a','','b']])

if __name__=='__main__':unittest.main()

class CleanupTests(unittest.TestCase):
    def meta(self):
        return {'sheets':[{'properties':{'sheetId':1,'title':m.SOURCE,'gridProperties':{'columnCount':8}}},
                          {'properties':{'sheetId':2,'title':m.DEST,'gridProperties':{'columnCount':8}}},
                          {'properties':{'sheetId':3,'title':'backup-active-abc'}},
                          {'properties':{'sheetId':4,'title':'backup-archive-abc'}},
                          {'properties':{'sheetId':5,'title':'backup-active-other'}}]}
    def test_blank_runs_deleted_descending_but_headers_and_formulas_kept(self):
        src=[['email','address'],[],['name','addr'],[],[],['=SUM(A1:A3)'],['',''],['next','addr2']]
        req,after,n,b=m.cleanup_plan(self.meta(),src,'abc')
        ranges=[r['deleteDimension']['range'] for r in req if 'deleteDimension' in r]
        self.assertEqual([(r['startIndex'],r['endIndex']) for r in ranges],[(6,7),(3,5)])
        self.assertEqual(n,3);self.assertEqual(b,2)
        self.assertEqual(after,[src[0],[],src[2],src[5],src[7]])
        self.assertEqual([r['deleteSheet']['sheetId'] for r in req if 'deleteSheet' in r],[3,4])
    def test_nonempty_hidden_column_and_empty_formula_not_deleted(self):
        req,after,n,b=m.cleanup_plan(self.meta(),[['header'],[],['','','note'],['=""']],'abc')
        self.assertEqual(n,0)
    def test_missing_backups_allowed(self):
        meta=self.meta();meta['sheets']=meta['sheets'][:2]
        self.assertEqual(m.cleanup_plan(meta,[['header'],[],['x']],'abc')[3],0)
    def test_signature_allows_formula_references_to_shift_not_literal_data(self):
        self.assertEqual(m.content_signature([['secret','=A9']]),m.content_signature([['secret','=A8']]))
        self.assertNotEqual(m.content_signature([['secret','=A9']]),m.content_signature([['changed','=A8']]))
        self.assertNotEqual(m.content_signature([['secret','=A9']]),m.content_signature([['secret','value']]))
    def test_completed_cleanup_does_not_call_api(self):
        from unittest.mock import Mock
        api=Mock();m.cleanup(api,{'state':'done','cleanup':'done'})
        api.metadata.assert_not_called();api.request.assert_not_called()
    def test_ambiguous_cleanup_does_not_resubmit(self):
        from unittest.mock import Mock
        api=Mock();api.metadata.return_value=self.meta();api.rows.return_value=[]
        with self.assertRaises(app.RemoteError):m.cleanup(api,{'state':'done','cleanup':'submitting','job':'abc'})
        api.request.assert_not_called()
