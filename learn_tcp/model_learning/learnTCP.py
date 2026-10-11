"""Compatibility entry point for TCP learning."""


def main():
    import sys
    from hydra.cli import main as cli

    return cli(["learn", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
