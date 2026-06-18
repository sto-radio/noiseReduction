import argparse
from pathlib import Path

from app.audio_pipeline import process_audio
from app.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean one audio file with DeepFilterNet3 and loudnorm.")
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, default=None)
    parser.add_argument("--basename", default=None)
    args = parser.parse_args()

    settings = get_settings()
    output_dir = args.output_dir or settings.output_dir
    work_dir = settings.work_dir / "cli"
    basename = args.basename or args.input.stem
    result = process_audio(args.input, output_dir, work_dir, basename, settings)
    print(result.output_path)


if __name__ == "__main__":
    main()
