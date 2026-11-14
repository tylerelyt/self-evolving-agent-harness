"""Install the pinned OpenViking server in a maintained, isolated environment."""
import argparse
from pathlib import Path
import subprocess
import sys

PINS=['openviking==0.4.12','openviking-sdk==0.1.1','xxhash==3.6.0','volcengine-python-sdk[ark]==5.0.3']

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',type=Path,default=Path(__file__).resolve().parents[1]/'var/openviking');p.add_argument('--wheelhouse',type=Path);p.add_argument('--check',action='store_true');a=p.parse_args()
    venv=a.state.resolve()/'venv';python=venv/'bin/python'
    if not a.check:
        if not python.exists():subprocess.run([sys.executable,'-m','venv',str(venv)],check=True)
        args=['--no-index','--find-links',str(a.wheelhouse.resolve())] if a.wheelhouse else ['--only-binary=:all:']
        subprocess.run([str(python),'-m','pip','install','--disable-pip-version-check','--no-compile',*args,*PINS],check=True)
    subprocess.run([str(python),'-c','from importlib.metadata import version; assert version("openviking")=="0.4.12"; print("OpenViking 0.4.12 ready")'],check=True)
if __name__=='__main__':main()
