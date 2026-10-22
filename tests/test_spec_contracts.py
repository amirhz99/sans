import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = ROOT / 'specs'
TEST_DIR = ROOT / 'tests'

# A Test Mapping entry starts with ``- AC-001`` and may continue on following
# lines.  Inside an entry a reference is ``tests/file.py[::Class[::method]]``;
# later bare ``test_name`` tokens inherit the last file and class.
ENTRY = re.compile(r'^-\s*(AC-\d{3}(?:\s*,\s*AC-\d{3})*)\s*->(.*?)(?=^-\s*AC-\d{3}|\Z)',
                   re.MULTILINE | re.DOTALL)
BACKTICK = re.compile(r'`([^`]+)`')
REFERENCE = re.compile(r'^(tests/[\w/]+\.py)(.*)$')
BARE_METHOD = re.compile(r'^(test_[A-Za-z_0-9]+)$')


def split_reference(token):
    """Return ``(path, class, method)`` for one backticked test reference.

    Older specs wrote ``file.py::Class.method`` while newer ones use
    ``file.py::Class::method``; both point at the same test.
    """
    reference = REFERENCE.match(token.strip())
    if not reference:
        return None
    path, rest = reference.groups()
    rest = rest[2:] if rest.startswith('::') else rest
    parts = [part for part in re.split(r'::|\.', rest) if part]
    klass = parts[0] if parts else None
    method = parts[1] if len(parts) > 1 else None
    return path, klass, method


def test_inventory():
    """Return {relative path: {class: {methods}}} parsed from the test sources."""
    found = {}
    for path in sorted(TEST_DIR.glob('test_*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        classes = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes[node.name] = {item.name for item in node.body
                                      if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
        found[f'tests/{path.name}'] = classes
    return found


def mapping_entries(content):
    """Return [(identifiers, [reference, ...])] for one specification."""
    section = content.split('## Test Mapping', 1)[1]
    entries = []
    for group, body in ENTRY.findall(section):
        references, last_file, last_class = [], None, None
        for token in BACKTICK.findall(body):
            parsed = split_reference(token)
            if parsed:
                path, klass, method = parsed
                last_file, last_class = path, klass
                references.append((path, klass, method))
            elif BARE_METHOD.match(token.strip()) and last_file:
                references.append((last_file, last_class, token.strip()))
        entries.append((re.findall(r'AC-\d{3}', group), references))
    return entries


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

    def test_every_acceptance_criterion_is_mapped(self):
        """A criterion without a mapping is an unverified promise."""
        for path in sorted(SPEC_DIR.glob('[0-9][0-9][0-9]-*.md')):
            content = path.read_text(encoding='utf-8')
            criteria = re.findall(r'^- \[[ x]\] (AC-\d{3}):', content, re.MULTILINE)
            mapped = {ident for identifiers, references in mapping_entries(content)
                      for ident in identifiers if references}
            self.assertEqual(set(criteria), mapped, f'{path.name}: unmapped criterion')

    def test_every_mapped_test_really_exists(self):
        """Renaming a test must break this suite, not only the reviewer's trust."""
        inventory = test_inventory()
        checked = 0
        for path in sorted(SPEC_DIR.glob('[0-9][0-9][0-9]-*.md')):
            content = path.read_text(encoding='utf-8')
            for identifiers, references in mapping_entries(content):
                for target, klass, method in references:
                    where = f"{path.name}: {', '.join(identifiers)}"
                    self.assertIn(target, inventory, f'{where}: unknown file {target}')
                    if not klass:
                        continue  # A whole-file reference is allowed but coarse.
                    classes = inventory[target]
                    if klass not in classes:
                        # ``file.py::test_name`` is the older shorthand: accept it
                        # only when the method really exists in some class.
                        self.assertTrue(klass.startswith('test_'),
                                        f'{where}: unknown class {target}::{klass}')
                        self.assertTrue(any(klass in methods for methods in classes.values()),
                                        f'{where}: missing test {target}::{klass}')
                        checked += 1
                        continue
                    if not method:
                        continue
                    self.assertIn(method, classes[klass],
                                  f'{where}: missing test {target}::{klass}::{method}')
                    checked += 1
        self.assertGreater(checked, 50)


if __name__ == '__main__':
    unittest.main()