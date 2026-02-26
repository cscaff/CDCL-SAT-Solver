"""
Download SATLIB benchmark tarballs (uf20, uf50, uf75) to bench/benchmarks/.
"""

import os
import tarfile
import urllib.request
import sys

BENCH_DIR = os.path.join(os.path.dirname(__file__), "benchmarks")

FAMILIES = {
    "uf20-91":  "https://www.cs.ubc.ca/~hoos/SATLIB/Benchmarks/SAT/RND3SAT/uf20-91.tar.gz",
    "uf50-218": "https://www.cs.ubc.ca/~hoos/SATLIB/Benchmarks/SAT/RND3SAT/uf50-218.tar.gz",
    "uf75-325": "https://www.cs.ubc.ca/~hoos/SATLIB/Benchmarks/SAT/RND3SAT/uf75-325.tar.gz",
}


def download_family(name: str):
    """Download and extract a SATLIB benchmark family."""
    url = FAMILIES.get(name)
    if url is None:
        print(f"Unknown family: {name}. Available: {list(FAMILIES.keys())}")
        return

    dest_dir = os.path.join(BENCH_DIR, name)
    if os.path.isdir(dest_dir) and any(f.endswith(".cnf") for f in os.listdir(dest_dir)):
        print(f"  {name}: already downloaded ({len([f for f in os.listdir(dest_dir) if f.endswith('.cnf')])} CNFs)")
        return

    os.makedirs(BENCH_DIR, exist_ok=True)
    tarball = os.path.join(BENCH_DIR, f"{name}.tar.gz")

    print(f"  Downloading {name} from {url} ...")
    urllib.request.urlretrieve(url, tarball)

    print(f"  Extracting to {dest_dir} ...")
    with tarfile.open(tarball, "r:gz") as tf:
        tf.extractall(BENCH_DIR, filter="data")

    # Clean up tarball
    os.remove(tarball)

    # If tarball extracted flat (no subdirectory), move .cnf files into dest_dir
    if not os.path.isdir(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        prefix = name.split("-")[0]  # e.g. "uf20"
        for f in os.listdir(BENCH_DIR):
            if f.startswith(prefix) and f.endswith(".cnf"):
                os.rename(os.path.join(BENCH_DIR, f),
                          os.path.join(dest_dir, f))

    cnf_count = len([f for f in os.listdir(dest_dir) if f.endswith(".cnf")])
    print(f"  {name}: {cnf_count} CNF instances extracted")


def download_all():
    """Download all benchmark families."""
    print("Downloading SATLIB benchmarks...")
    for name in FAMILIES:
        download_family(name)
    print("Done.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        for name in sys.argv[1:]:
            download_family(name)
    else:
        download_all()
