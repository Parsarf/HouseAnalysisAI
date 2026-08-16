import time

from .worker import default_worker


def main() -> None:
    worker = default_worker()
    while True:
        worked = worker.run_once()
        if not worked:
            time.sleep(1)


if __name__ == "__main__":
    main()
