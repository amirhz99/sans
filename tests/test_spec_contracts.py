import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = ROOT / 'specs'


class SpecContractTests(unittest.TestCase):
    def test_agent_guidance_and_sdd_templates_exist(self):
        for path in (ROOT / 'AGENTS.md', SPEC_DIR / 'README.md', SPEC_DIR / 'template.md'):
            self.assertTrue(path.is_file(), path)

    def test_numbered_specs_have_required_sections_and_status(self):
        specs = sorted(SPEC_DIR.glob('[0-9][0-9][0-9]-*.md'))
        self.assertGreaterEqual(len(specs), 2)
        required = (
            '## Problem', '## Goal', '## Scope', '## Non-goals', '## Contracts',
            '## Acceptance Criteria', '## Test Mapping', '## Safety Constraints',
            '## Validation',
        )
        valid_statuses = {'draft', 'ready', 'in_progress', 'verified', 'superseded'}
        for path in specs:
            content = path.read_text(encoding='utf-8')
            self.assertRegex(content, r'^# SPEC-\d{3}: .+', path)
            status = re.search(r'^Status:\s*(\w+)\s*$', content, re.MULTILINE)
            self.assertIsNotNone(status, path)
            self.assertIn(status.group(1), valid_statuses, path)
            for section in required:
                self.assertIn(section, content, f'{section} missing from {path}')
            criteria = re.findall(r'^- \[[ x]\] (AC-\d{3}):', content, re.MULTILINE)
            self.assertTrue(criteria, f'No acceptance criteria in {path}')
            self.assertEqual(len(criteria), len(set(criteria)), f'Duplicate criterion in {path}')
            self.assertRegex(content, r'`tests/[^`]+`', path)


if __name__ == '__main__':
    unittest.main()