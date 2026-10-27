"""Distribution-layout checks without touching Home Assistant."""
from pathlib import Path
import unittest
import yaml
import jarvis

ROOT = Path(__file__).resolve().parents[1]


class RepositoryTests(unittest.TestCase):
    def test_store_layout_and_consistent_versions(self):
        repository = yaml.safe_load((ROOT / 'repository.yaml').read_text())
        config = yaml.safe_load((ROOT / 'ha_jarvis/config.yaml').read_text())
        self.assertEqual(repository['url'], 'https://github.com/dylanhelp-cmyk/ha-jarvis')
        self.assertEqual(config['slug'], 'ha_jarvis')
        self.assertEqual(config['version'], jarvis.__version__)
        self.assertFalse((ROOT / 'config.yaml').exists())
        for name in ('Dockerfile', 'requirements.txt', 'jarvis/server.py', 'firmware/voice-pe-repository.yaml'):
            self.assertTrue((ROOT / 'ha_jarvis' / name).is_file(), name)

    def test_remote_firmware_package_has_no_secret_lookups(self):
        firmware = ROOT / 'ha_jarvis/firmware'
        for name in ('voice-pe-package.yaml', 'voice-pe-hardware.yaml'):
            self.assertNotIn('!secret', (firmware / name).read_text())
        entry = (firmware / 'voice-pe-repository.yaml').read_text()
        self.assertIn('ha_jarvis/firmware/components', entry)
        self.assertIn('ha_jarvis/firmware/voice-pe-package.yaml', entry)
        self.assertIn('!secret jarvis_pairing_token', entry)


if __name__ == '__main__': unittest.main()
