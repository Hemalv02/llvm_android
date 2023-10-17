#!/usr/bin/env python3
import argparse
import sys

def main():
    parser = argparse.ArgumentParser(
        description='Check if the cherrypick_cl.py script has been run')
    parser.add_argument('commit_msg', type=str, help='commit message')
    parser.add_argument('commit_files', type=str, nargs='*',
                       help='files changed in the commit')
    args = parser.parse_args()

    commit_msg = args.commit_msg
    commit_files = args.commit_files

    patch_list = []
    for file in commit_files:
        if file.endswith('.patch'):
            patch_list.append(file)

    if len(patch_list) == 0:
        return 0

    for line in commit_msg.splitlines():
        line = line.strip()
        if line.startswith("This change is generated automatically by the script"):
            return 0

    print('This change seems to manually add/change the following patch files:')
    for file in patch_list:
        print(file)
    print('Please run llvm_android/cherrypick_cl.py script to cherry pick changes instead.')

    return 1

if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
