import os
import sys
import json
import requests
import subprocess
import shutil
import stat
import errno
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QLineEdit, QPushButton, QComboBox, QFileDialog, QMessageBox
)
from PyQt6.QtGui import QPixmap, QIcon
from PyQt6.QtCore import Qt, QThread, pyqtSignal

# Configuration
REPO_JSON_URL = "https://arduboy.ried.cl/repo.json"
ARDUINO_CLI = "arduino-cli"  # Ensure it's in your system's PATH
ARDUINO_BOARD = "arduboy-homemade:avr:arduboy-homemade"

class FetchThread(QThread):
    """Thread to fetch the repo.json file."""
    fetched = pyqtSignal(list)

    def run(self):
        try:
            response = requests.get(REPO_JSON_URL, timeout=10)
            response.raise_for_status()
            data = response.json()
            if "items" not in data or not isinstance(data["items"], list):
                raise ValueError("Invalid repo.json format.")
            self.fetched.emit(data["items"])
        except Exception as e:
            print(f"Error fetching repo.json: {e}")
            self.fetched.emit([])

class CloneThread(QThread):
    """Thread to handle repository cloning."""
    finished = pyqtSignal(bool, str)  # Signal to indicate cloning status and message

    def __init__(self, repo_url, clone_path):
        super().__init__()
        self.repo_url = repo_url
        self.clone_path = clone_path

    def run(self):
        try:
            subprocess.run(["git", "clone", self.repo_url, str(self.clone_path)], check=True)
            self.finished.emit(True, "Cloning completed.")
        except subprocess.CalledProcessError as e:
            self.finished.emit(False, f"Failed to clone repository: {e}")

class CompileThread(QThread):
    """Thread to handle sketch compilation."""
    finished = pyqtSignal(bool, str)  # Signal to indicate compilation status and message

    def __init__(self, sketch_path, build_flags, build_path, compile_path):
        super().__init__()
        self.sketch_path = sketch_path
        self.build_flags = build_flags
        self.build_path = build_path
        self.compile_path = compile_path

    def run(self):
        try:
            result = subprocess.run(
                [ARDUINO_CLI, "compile", "--fqbn", ARDUINO_BOARD, "--build-path", str(self.build_path), *self.build_flags, str(self.compile_path)],
                check=True,
                capture_output=True,
                text=True
            )

            # Locate the compiled binary (matches Arduino IDE naming)
            sketch_name = self.compile_path.name
            compiled_binary = self.build_path / f"{sketch_name}.ino.hex"
            if not compiled_binary.exists():
                hex_files = list(self.build_path.glob("*.hex"))
                if hex_files:
                    compiled_binary = hex_files[0]
                else:
                    self.finished.emit(False, "No .hex file found in build directory.")
                    return

            self.finished.emit(True, str(compiled_binary))
        except subprocess.CalledProcessError as e:
            self.finished.emit(False, f"Failed to compile sketch: {e}\nOutput:\n{e.stdout}\nError:\n{e.stderr}")

class ArduboyManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Arduboy ReComp")
        self.setGeometry(100, 100, 800, 600)

        # Set window icon
        icon_path = self.resource_path("arduboy_icon.ico")
        self.setWindowIcon(QIcon(icon_path))

        # Main layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)

        # Search bar
        self.search_bar = QLineEdit(self)
        self.search_bar.setPlaceholderText("Search sketches...")
        self.search_bar.textChanged.connect(self.filter_sketches)
        self.layout.addWidget(self.search_bar)

        # List widget
        self.list_widget = QListWidget(self)
        self.list_widget.setIconSize(QPixmap(100, 100).size())
        self.layout.addWidget(self.list_widget)

        # Settings panel
        self.settings_panel = QVBoxLayout()

        # Variant
        self.variant_combo = QComboBox(self)
        self.variant_combo.addItems([
            "Arduino Leonardo",
            "Arduino/Genuino Micro",
            "Pro Micro 5V Standard Wiring",
            "Arduino Pro Micro Alternate Wiring"
        ])
        self.settings_panel.addWidget(QLabel("Variant:"))
        self.settings_panel.addWidget(self.variant_combo)

        # Display
        self.display_combo = QComboBox(self)
        self.display_combo.addItems(["SH1106", "SSD1306", "SSD1309"])
        self.settings_panel.addWidget(QLabel("Display:"))
        self.settings_panel.addWidget(self.display_combo)

        # Flash Chip
        self.flash_combo = QComboBox(self)
        self.flash_combo.addItems(["Pin2/D1/SDA", "Pin0/D0/Rx"])
        self.settings_panel.addWidget(QLabel("Flash Chip:"))
        self.settings_panel.addWidget(self.flash_combo)

        self.layout.addLayout(self.settings_panel)

        # Buttons
        button_layout = QHBoxLayout()
        self.fetch_button = QPushButton("Fetch Sketches", self)
        self.fetch_button.clicked.connect(self.fetch_sketches)
        button_layout.addWidget(self.fetch_button)

        self.add_local_button = QPushButton("Add Local Sketch", self)
        self.add_local_button.clicked.connect(self.add_local_sketch)
        button_layout.addWidget(self.add_local_button)

        self.compile_button = QPushButton("Compile Selected Sketch", self)
        self.compile_button.clicked.connect(self.compile_sketch)
        button_layout.addWidget(self.compile_button)

        self.layout.addLayout(button_layout)

        # Status label
        self.status_label = QLabel("", self)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("font-weight: bold; color: blue;")
        self.layout.addWidget(self.status_label)

        # Data
        self.sketches = []

    def resource_path(self, relative_path):
        """Get the absolute path to a resource, works for development and for PyInstaller."""
        try:
            base_path = sys._MEIPASS
        except Exception:
            base_path = os.path.abspath(".")

        return os.path.join(base_path, relative_path)

    def fetch_sketches(self):
        """Fetch sketches from the repo.json file."""
        self.fetch_thread = FetchThread()
        self.fetch_thread.fetched.connect(self.populate_list)
        self.fetch_thread.start()

    def populate_list(self, sketches):
        """Populate the list widget with fetched sketches."""
        self.sketches = sketches
        self.list_widget.clear()
        for sketch in sketches:
            item = QListWidgetItem(sketch.get("title", "Untitled"))
            item.setData(Qt.ItemDataRole.UserRole, sketch)
            if "thumbnailUrl" in sketch:
                thumbnail = QPixmap()
                thumbnail.loadFromData(requests.get(sketch["thumbnailUrl"]).content)
                item.setIcon(QIcon(thumbnail))
            self.list_widget.addItem(item)

    def filter_sketches(self):
        """Filter sketches based on search text."""
        search_text = self.search_bar.text().lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            sketch = item.data(Qt.ItemDataRole.UserRole)
            match = search_text in sketch.get("title", "").lower() or search_text in sketch.get("description", "").lower()
            item.setHidden(not match)

    def get_build_flags(self):
        """Generate build flags based on selected settings."""
        variant = self.variant_combo.currentText()
        display = self.display_combo.currentText()
        flash = self.flash_combo.currentText()

        # Map variant to correct flags
        variant_flags = {
            "Arduino Leonardo": "-DARDUBOY_LEONARDO",
            "Arduino/Genuino Micro": "-DARDUBOY_MICRO",
            "Pro Micro 5V Standard Wiring": "-DARDUBOY_PRO_MICRO",
            "Arduino Pro Micro Alternate Wiring": "-DARDUBOY_PRO_MICRO -DAB_ALTERNATE_WIRING"
        }.get(variant, "-DARDUBOY_PRO_MICRO")

        # Map display to correct OLED flags
        display_flags = {
            "SH1106": "-DOLED_SH1106",
            "SSD1306": "-DOLED_SSD1306",
            "SSD1309": "-DOLED_SSD1309"
        }.get(display, "-DOLED_SSD1306")

        # Map flash chip to correct CS pin flags
        flash_flags = {
            "Pin2/D1/SDA": "-DCART_CS_SDA",
            "Pin0/D0/Rx": "-DCART_CS_RX"
        }.get(flash, "-DCART_CS_SDA")

        return (
            "--build-property", "build.extra_flags="
            f"{variant_flags} "
            f"{display_flags} "
            f"{flash_flags} "
            f"-DUSB_VID=0x2341 "  # Default VID
            f"-DUSB_PID=0x8036"   # Default PID
        )

    def add_local_sketch(self):
        """Open a file dialog to select a local .ino sketch and add it to the list."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Sketch", "", "Arduino Sketches (*.ino)"
        )
        if file_path:
            sketch_path = Path(file_path)
            sketch_data = {
                'title': sketch_path.stem,
                'local_path': str(sketch_path),
                'type': 'local'
            }
            item = QListWidgetItem(sketch_data['title'])
            item.setData(Qt.ItemDataRole.UserRole, sketch_data)
            self.list_widget.addItem(item)

    def compile_sketch(self):
        """Compile the selected sketch (either local or remote)."""
        selected_item = self.list_widget.currentItem()
        if not selected_item:
            QMessageBox.warning(self, "Error", "No sketch selected.")
            return

        sketch = selected_item.data(Qt.ItemDataRole.UserRole)
        if sketch.get('type') == 'local':
            self.compile_local_sketch(sketch)
        else:
            self.compile_remote_sketch(sketch)

    def compile_local_sketch(self, sketch):
        """Handle compilation for a local sketch."""
        local_path = Path(sketch.get('local_path'))
        if not local_path.exists():
            QMessageBox.warning(self, "Error", "Local sketch file not found.")
            return

        parent_dir = local_path.parent
        sketch_name = local_path.stem
        temp_dir = None

        # Check if the sketch is in a correctly named directory
        if parent_dir.name != sketch_name:
            temp_dir = Path("temp_local_compile")
            if temp_dir.exists():
                shutil.rmtree(temp_dir, onerror=self.handle_remove_readonly)
            temp_dir.mkdir(exist_ok=True)
            sketch_temp_dir = temp_dir / sketch_name
            sketch_temp_dir.mkdir(exist_ok=True)

            # Copy all files to temp directory
            for file in parent_dir.iterdir():
                if file.is_file():
                    shutil.copy(file, sketch_temp_dir)

            # Rename .ino file to match directory
            new_ino_path = sketch_temp_dir / f"{sketch_name}.ino"
            original_ino = sketch_temp_dir / local_path.name
            if original_ino.exists():
                original_ino.rename(new_ino_path)
            else:
                QMessageBox.warning(self, "Error", "Sketch file missing in temp directory.")
                return
            compile_path = sketch_temp_dir
            sketch_path = new_ino_path
        else:
            compile_path = parent_dir
            sketch_path = local_path

        self.status_label.setText("Compiling...")
        build_path = compile_path / "build"
        build_path.mkdir(exist_ok=True)
        build_flags = self.get_build_flags()

        # Start compilation thread with cleanup for temp directory
        self.compile_thread = CompileThread(sketch_path, build_flags, build_path, compile_path)
        self.compile_thread.finished.connect(
            lambda success, msg: self.handle_compile_finished(success, msg, temp_dir)
        )
        self.compile_thread.start()

    def compile_remote_sketch(self, sketch):
        """Handle compilation for a remote (cloned) sketch."""
        source_url = sketch.get("sourceUrl")
        if not source_url:
            QMessageBox.warning(self, "Error", "No source URL found for this sketch.")
            return

        self.status_label.setText("Cloning...")
        clone_path = Path("temp_clone")
        if clone_path.exists():
            shutil.rmtree(clone_path, onerror=self.handle_remove_readonly)

        self.clone_thread = CloneThread(source_url, clone_path)
        self.clone_thread.finished.connect(self.handle_clone_finished)
        self.clone_thread.start()

    def handle_clone_finished(self, success, message):
        """Handle the result of the cloning thread."""
        if not success:
            QMessageBox.warning(self, "Error", message)
            self.status_label.setText("")
            return

        self.status_label.setText("Compiling...")
        sketch_path = self.find_sketch_file(Path("temp_clone"))
        if not sketch_path:
            QMessageBox.warning(self, "Error", "No sketch file found in the repository.")
            self.status_label.setText("")
            return

        sketch_path = self.rename_sketch_file(sketch_path)
        compile_path = sketch_path.parent
        build_path = compile_path / "build"
        build_path.mkdir(exist_ok=True)
        build_flags = self.get_build_flags()

        self.compile_thread = CompileThread(sketch_path, build_flags, build_path, compile_path)
        self.compile_thread.finished.connect(
            lambda success, msg: self.handle_compile_finished(success, msg, Path("temp_clone"))
        )
        self.compile_thread.start()

    def handle_compile_finished(self, success, message, temp_dir=None):
        """Handle compilation completion with optional temp directory cleanup."""
        if success:
            compiled_binary = Path(message)
            self.export_binary(compiled_binary)
        else:
            QMessageBox.warning(self, "Error", message)
        self.status_label.setText("")
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, onerror=self.handle_remove_readonly)

    def export_binary(self, compiled_binary):
        """Export the compiled binary."""
        if not compiled_binary.exists():
            QMessageBox.warning(self, "Error", f"Compiled binary not found: {compiled_binary}")
            return

        default_name = f"{compiled_binary.parent.parent.name}.hex"
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save Binary", default_name, "Hex Files (*.hex)"
        )
    
        if file_path:
            try:
                shutil.copy(compiled_binary, file_path)
                QMessageBox.information(self, "Success", f"Binary saved to {file_path}")
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to save binary: {e}")

    def handle_remove_readonly(self, func, path, exc_info):
        """Handle read-only files during directory removal."""
        if func in (os.rmdir, os.remove, os.unlink) and exc_info[1].errno == errno.EACCES:
            os.chmod(path, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
            func(path)
        else:
            raise exc_info[1]

    def find_sketch_file(self, directory):
        """Search for a sketch file containing setup() and loop()."""
        for root, _, files in os.walk(directory):
            for file in files:
                if file.endswith(".ino"):
                    sketch_path = Path(root) / file
                    try:
                        with open(sketch_path, "r", encoding="utf-8") as f:
                            content = f.read().lower()
                            if "setup(" in content and "loop(" in content:
                                return sketch_path
                    except Exception as e:
                        print(f"Error reading {sketch_path}: {e}")
        return None

    def rename_sketch_file(self, sketch_path):
        """Rename the sketch file to match its parent directory name."""
        parent_dir = sketch_path.parent
        new_name = parent_dir.name + ".ino"
        new_path = parent_dir / new_name

        if sketch_path.name != new_name:
            try:
                sketch_path.rename(new_path)
                return new_path
            except Exception as e:
                print(f"Error renaming sketch file: {e}")
                return sketch_path
        return sketch_path

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ArduboyManager()
    window.show()
    sys.exit(app.exec())
