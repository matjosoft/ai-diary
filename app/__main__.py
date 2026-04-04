import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="AI Diary")
    parser.add_argument(
        "--process",
        type=Path,
        metavar="AUDIO_FILE",
        help="Process an existing audio file through the pipeline (transcribe → analyze → store)",
    )
    parser.add_argument("--host", default=None, help="Server host")
    parser.add_argument("--port", type=int, default=None, help="Server port")
    args = parser.parse_args()

    if args.process:
        process_file(args.process)
    else:
        run_server(args.host, args.port)


def process_file(audio_path: Path):
    if not audio_path.exists():
        print(f"Error: {audio_path} not found")
        sys.exit(1)

    from app.database import init_db
    from app.services.pipeline import process_audio

    init_db()
    print(f"Processing {audio_path}")
    process_audio(audio_path)


def run_server(host: str | None, port: int | None):
    import uvicorn
    from app.config import settings

    uvicorn.run(
        "app.main:app",
        host=host or settings.HOST,
        port=port or settings.PORT,
    )


if __name__ == "__main__":
    main()
