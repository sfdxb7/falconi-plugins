"""Run with python3 -m unittest discover -s plugins/garmoosha-screen."""
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


class ChromiumLauncherTest(unittest.TestCase):
    def test_single_and_nested_launchers(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            g = root / 'garmoosha-screen' / 'chromium'
            m = root / 'murmur-screen' / 'chromium'
            g.parent.mkdir()
            m.parent.mkdir()
            shutil.copy2(Path(__file__).with_name('chromium'), g)
            # Model the Murmur launcher's executable-selector and flag contract.
            m.write_text('''#!/bin/sh
[ "${1:-}" = --murmur-browser ] || exit 126
browser=$2; shift 2
if [ -n "${DISPLAY:-}" ]; then
  set -- --start-maximized --hide-crash-restore-bubble "$@"
else
  set -- --headless=new "$@"
fi
exec "$browser" --test-type --disable-infobars "$@"
''')
            m.chmod(0o755)
            xprop = root / 'xprop'
            xprop.write_text('''#!/bin/sh
case "$1" in -root) echo 'window id # 0xa';; *) echo '"test window manager"';; esac
''')
            xprop.chmod(0o755)
            recorder = root / 'recorder'
            recorder.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            recorder.chmod(0o755)
            for display, flags in [('', ['--headless=new']), (':77', ['--start-maximized', '--hide-crash-restore-bubble'])]:
                for argv in ([g, '--garmoosha-browser'], [g, '--garmoosha-browser', m, '--murmur-browser'], [m, '--murmur-browser', g, '--garmoosha-browser']):
                    with self.subTest(display=display, argv=argv):
                        env = {**os.environ, 'DISPLAY': display, 'PATH': f'{root}:{os.environ["PATH"]}', 'XDG_CONFIG_HOME': '', 'GARMOOSHA_SCREEN_BROWSER': ''}
                        result = subprocess.run([*argv, recorder, 'marker with spaces'], env=env, text=True, capture_output=True, timeout=5)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.splitlines(), ['--test-type', '--disable-infobars', *flags, 'marker with spaces'])
