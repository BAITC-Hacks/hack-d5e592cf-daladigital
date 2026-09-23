"""Export the fully synthetic fixture through the real application exporter."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import exports


def main():
    directory = ROOT / 'docs/examples'
    meeting = json.loads((directory / 'sample-protocol.json').read_text())
    os.environ['PROTOCOL_ORGANIZATION'] = 'Учебная организация'
    (directory / 'sample-protocol.docx').write_bytes(exports.docx(meeting))
    (directory / 'sample-protocol.pdf').write_bytes(exports.pdf(meeting))
    print('Synthetic DOCX/PDF written to docs/examples')


if __name__ == '__main__':
    main()
