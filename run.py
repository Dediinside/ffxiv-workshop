"""Local Python application with a modern browser interface."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'modules' / name) for name in ('leve', 'market', 'reseller')]


def main():
    parser = argparse.ArgumentParser(description='FFXIV Мастерская')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args()
    from workshop.server import serve
    from workshop.profile import profile_home
    folder = args.profile or (profile_home() / 'demo' if args.demo else profile_home())
    serve(folder, args.demo, args.port, not args.no_browser)


if __name__ == '__main__':
    main()
