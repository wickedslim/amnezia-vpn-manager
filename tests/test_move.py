import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from manager.domain import ManagerError, Server
from manager.move import Moves, move_target


class MoveTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_ip_and_rejects_other_arguments(self):
        self.assertEqual(move_target('2001:db8:0::1', 22), '[2001:db8::1]:22')
        for host, port in [('-oProxyCommand=x', 22), ('example.com', 22), ('fe80::1%eth0', 22), ('192.0.2.1', True), ('192.0.2.1', 65536)]:
            with self.subTest(host=host, port=port), self.assertRaises(ManagerError):
                move_target(host, port)

    async def test_checks_then_starts_separate_unit_for_confirmed_target_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'scripts/awg-move.sh'
            script.parent.mkdir()
            script.touch()
            server = Server(id=1, name='Original', local=True)
            moves = Moves(root, SimpleNamespace(server=lambda sid: server))
            moves.active = AsyncMock(return_value=False)
            moves.run = AsyncMock(side_effect=[
                subprocess.CompletedProcess([], 0, stdout=str(root / 'awg')+'\n'),
                subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
            ])
            with patch('manager.move.sys.platform', 'linux'), patch('manager.move.os.geteuid', return_value=0):
                plan = await moves.prepare(1, '192.0.2.2', 2222)
                self.assertEqual(plan['target'], '192.0.2.2:2222')
                self.assertEqual(moves.run.await_args.args[0], ['bash', str(script), plan['target'], '--batch', '--check'])
                with self.assertRaises(ManagerError):
                    await moves.start(1, 'different')
                self.assertTrue((await moves.start(1, plan['nonce']))['scheduled'])
                argv = moves.run.await_args.args[0]
                self.assertEqual(argv[0], 'systemd-run')
                self.assertEqual(argv[-4:], [str(script), plan['target'], '--batch', '--yes'])
                with self.assertRaises(ManagerError):
                    await moves.start(1, plan['nonce'])

    async def test_remote_server_rejected_without_running_commands(self):
        moves = Moves(Path('/tmp'), SimpleNamespace(server=lambda sid: Server(id=sid, name='Remote')))
        moves.run = AsyncMock()
        with self.assertRaises(ManagerError):
            await moves.prepare(1, '192.0.2.2', 22)
        moves.run.assert_not_awaited()
