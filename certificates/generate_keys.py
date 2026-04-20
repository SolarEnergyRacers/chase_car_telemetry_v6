

from argparse import ArgumentParser
import os.path
from pathlib import Path
import zmq.auth

parser = ArgumentParser(description="Generate ZeroMQ CURVE keypair.")
parser.add_argument("--force", "-f", action="store_true", 
    help="overwrite/add without asking")
parser.add_argument("--name", default="certificate", 
    help="basename for client keypair (default: certificate)")

ROOT = Path(__file__).parent

def yN_query(text: str):
    inp = None
    while not(inp in ['y', 'n']):
        inp = input(text + " (y/N): ").lower()
    return (inp == 'y')

def main():
    args = parser.parse_args()
    name = args.name
    force = args.force

    mine_path = ROOT / "mine"
    public  = mine_path / f"{name}.key"
    private = mine_path / f"{name}.key_secret"
    pub2, priv2 = [os.path.split(x)[1] for x in (public, private)]
    if public.is_file() or private.is_file() and not(force):
        msg  = f"This key already exists, overwrite? This will "
        msg += f"invalidate all existing connections based on {pub2}"
        if not yN_query(msg):
            print("aborted on user input")
            return -1
    elif any(mine_path.glob("*.key")) or any(mine_path.glob("*.key_secret")):
        if not force:
            msg = "Some key already exists, add new?"
            if not yN_query(msg):
                print("aborted on user input")
                return -1

    zmq.auth.create_certificates(mine_path, name)
    fp_common = os.path.commonpath([public, private])
    print(f"generated {pub2}/{priv2} in {mine_path}")
    return 0


if __name__ == "__main__":
    exit(main())
