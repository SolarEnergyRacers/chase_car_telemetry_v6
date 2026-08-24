

import queue

from gui_stuff.gui_main import main_st

from backend.data_hoarder import pipe


if __name__ == "__main__":
    main_st()

    while True:
        try:
            print(f"pipe got {pipe.get(block=False)}")
        except queue.Empty:
            break
