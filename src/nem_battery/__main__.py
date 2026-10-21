from nem_battery.cli import main

if __name__ == "__main__":  # guard needed: worker processes re-import this module on Windows/macOS
    main()
