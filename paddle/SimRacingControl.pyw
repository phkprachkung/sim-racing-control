import os
import sys

# Ensure current working directory is set to script folder
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from arduino_vjoy import App

if __name__ == "__main__":
    app = App()
    app.mainloop()
