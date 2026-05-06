import json
import sys
import os
import glob


def format_trace(input_path, output_path=None):
    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_formatted{ext}"

    with open(input_path, "r", encoding="utf-8") as f:
        raw = f.read()

    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder(strict=False)
        try:
            data, _ = decoder.raw_decode(raw)
            print(f"  Warning: JSON incomplete, parsed partial data ({len(data)} messages)")
        except json.JSONDecodeError as e:
            print(f"  Error: Failed to parse: {e}")
            return False

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    print(f"  -> {output_path}")
    return True


def format_traces_in_dir(dir_path):
    pattern = os.path.join(dir_path, "*_trace.txt")
    files = sorted(glob.glob(pattern))
    formatted_files = [f for f in files if "_formatted" not in f]

    if not formatted_files:
        print(f"No trace files found in {dir_path}")
        return

    print(f"Found {len(formatted_files)} trace file(s) in {dir_path}")
    success = 0
    for f in formatted_files:
        print(f"Processing: {os.path.basename(f)}")
        if format_trace(f):
            success += 1

    print(f"\nDone: {success}/{len(formatted_files)} formatted successfully")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python format_trace.py <trace_file> [output_file]   - format single file")
        print("  python format_trace.py <traces_directory>           - batch format all trace files")
        sys.exit(1)

    path = sys.argv[1]
    if os.path.isdir(path):
        format_traces_in_dir(path)
    elif os.path.isfile(path):
        format_trace(path, sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print(f"Error: {path} not found")
        sys.exit(1)
