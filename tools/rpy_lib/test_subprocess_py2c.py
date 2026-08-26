import subprocess
import tempfile
import os

def main():
    r = subprocess.run(["echo", "hello world"], capture_output=True, text=True)
    print("rc=" + str(r.returncode) + " out=" + r.stdout.strip())
    r2 = subprocess.run(["sh", "-c", "echo E >&2; exit 3"], capture_output=True, text=True)
    print("rc2=" + str(r2.returncode) + " err=" + r2.stderr.strip())
    d = tempfile.mkdtemp(prefix="sptest-")
    r3 = subprocess.run(["pwd"], capture_output=True, text=True, cwd=d)
    if r3.stdout.strip() == d:
        print("cwd ok")
    big = subprocess.run(["sh", "-c", "seq 1 5000"], capture_output=True, text=True)
    print("biglines=" + str(len(big.stdout.strip().split("\n"))))
main()
