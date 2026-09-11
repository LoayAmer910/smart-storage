import difflib, time, sys

base_path = sys.argv[1]
orig_path = sys.argv[2]
slice_size = int(sys.argv[3])

with open(base_path, 'rb') as f:
    base = f.read(slice_size)
with open(orig_path, 'rb') as f:
    orig = f.read(slice_size)

print(f"comparing {len(base)} vs {len(orig)} bytes")
t0 = time.time()
matcher = difflib.SequenceMatcher(None, base, orig, autojunk=False)
ops = matcher.get_opcodes()
t1 = time.time()
print(f"took {t1-t0:.3f}s, {len(ops)} ops")
