import os
import sys
import json
import requests
import subprocess
import shutil
import stat
import errno
import csv
from io import StringIO
from pathlib import Path
from bs4 import BeautifulSoup
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QLineEdit, QPushButton, QComboBox, QFileDialog, QMessageBox
)
from PyQt6.QtGui import QPixmap, QIcon, QFont, QColor
from PyQt6.QtCore import Qt, QThread, pyqtSignal

# Configuration
REPO_JSON_URL = "https://arduboy.ried.cl/repo.json"
BIGFX_REPO_URL = "https://www.bloggingadeadhorse.com/cart/Cart_GetList.php?listId=1&filename=full"
BIGFX_BASE_URL = "https://www.bloggingadeadhorse.com/cart/"

ARDUINO_CLI = "arduino-cli"
ARDUINO_BOARD = "arduboy-homemade:avr:arduboy-homemade"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
}

class FetchThread(QThread):
    """Thread to fetch sketches from multiple sources."""
    fetched = pyqtSignal(list)

    def run(self):
        all_items = []
        
        # Fetch main repository
        try:
            response = requests.get(REPO_JSON_URL, headers=HEADERS, timeout=15)
            response.raise_for_status()
            data = response.json()
            if "items" in data and isinstance(data["items"], list):
                all_items.extend(data["items"])
        except Exception as e:
            print(f"Error fetching main repo: {e}")

        # Fetch and parse BigFX CSV data
        try:
            response = requests.get(BIGFX_REPO_URL, headers=HEADERS, timeout=15)
            response.raise_for_status()
            
            # Parse CSV data
            csv_data = csv.DictReader(StringIO(response.text), delimiter=';')
            for row in csv_data:
                # Skip entries without valid source code
                source = row.get('Source', '').strip().lower()
                if not source or source in {'na', 'n/a', 'none', ''}:
                    continue
                
                try:
                    transformed = {
                        "title": row.get('Title', 'Untitled').strip(),
                        "sourceUrl": self.clean_git_url(source),
                        "thumbnailUrl": row.get('Image', '').strip(),
                        "description": row.get('Description', '').strip(),
                        "type": "bigfx"
                    }
                    if transformed["sourceUrl"]:
                        all_items.append(transformed)
                except Exception as e:
                    print(f"Error processing row {row}: {e}")

        except Exception as e:
            print(f"Error processing BigFX data: {e}")

        # Remove duplicates based on normalized sourceUrl
        unique_sketches = {}
        for sketch in all_items:
            source_url = self.normalize_url(sketch.get("sourceUrl"))
            if source_url:
                unique_sketches[source_url] = sketch

        self.fetched.emit(list(unique_sketches.values()))

    def normalize_url(self, url):
        """Normalize Git URLs to prevent duplicates."""
        if not url:
            return None
        return url.lower().replace(".git", "").strip()

    def clean_git_url(self, url):
        """Validate and clean Git URLs."""
        from urllib.parse import urlparse
        
        # Clean up URL formatting
        url = url.strip()
        if not url:
            return ""
            
        parsed = urlparse(url)
        
        # Add scheme if missing
        if not parsed.scheme:
            url = f"https://{url}"
            parsed = urlparse(url)  # Re-parse with scheme
        
        # Handle GitHub URLs
        if "github.com" in parsed.netloc:
            # Convert tree URLs to raw repo URLs
            if "/tree/" in parsed.path:
                path_parts = parsed.path.split("/tree/")
                return f"{parsed.scheme}://{parsed.netloc}{path_parts[0]}.git"
            
            # Add .git extension if missing
            if not parsed.path.endswith(".git"):
                return f"{parsed.scheme}://{parsed.netloc}{parsed.path}.git"
        
        return url
    def transform_bigfx_data(self, bigfx_data):
        """Transform BigFX data to match standard repo format."""
        transformed = []
        for row in bigfx_data:
            try:
                # Convert relative thumbnail URLs to absolute
                thumbnail = row.get('Image', '').strip()
                if thumbnail and not thumbnail.startswith(('http:', 'https:')):
                    thumbnail = f"{BIGFX_BASE_URL}{thumbnail}"

                transformed.append({
                    "title": row.get('Title', 'Untitled').strip(),
                    "sourceUrl": self.clean_git_url(row['Source'].strip()),
                    "thumbnailUrl": thumbnail,
                    "description": row.get('Description', '').strip(),
                    "type": "bigfx"
                })
            except KeyError as e:
                print(f"Missing key in row: {e}")
                continue
        return transformed 

class CloneThread(QThread):
    """Thread to handle repository cloning."""
    finished = pyqtSignal(bool, str)

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
    finished = pyqtSignal(bool, str)

    def __init__(self, sketch_path, build_flags, build_path, compile_path):
        super().__init__()
        self.sketch_path = sketch_path
        self.build_flags = build_flags
        self.build_path = build_path
        self.compile_path = compile_path

    def run(self):
        try:
            result = subprocess.run(
                [ARDUINO_CLI, "compile", "--fqbn", ARDUINO_BOARD, 
                 "--build-path", str(self.build_path), *self.build_flags, 
                 str(self.compile_path)],
                check=True,
                capture_output=True,
                text=True
            )

            # Locate compiled binary
            sketch_name = self.compile_path.name
            compiled_binary = self.build_path / f"{sketch_name}.ino.hex"
            if not compiled_binary.exists():
                hex_files = list(self.build_path.glob("*.hex"))
                compiled_binary = hex_files[0] if hex_files else None

            if compiled_binary:
                self.finished.emit(True, str(compiled_binary))
            else:
                self.finished.emit(False, "No .hex file found in build directory.")
        except subprocess.CalledProcessError as e:
            error_msg = f"Failed to compile sketch: {e}\nOutput:\n{e.stdout}\nError:\n{e.stderr}"
            self.finished.emit(False, error_msg)

class ArduboyManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Arduboy ReComp")
        self.setGeometry(100, 100, 800, 600)
        self.setWindowIcon(QIcon(self.resource_path("arduboy_icon.ico")))

        # Main UI components
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)

        # Search and list widgets
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Search sketches...")
        self.search_bar.textChanged.connect(self.filter_sketches)
        self.layout.addWidget(self.search_bar)

        self.list_widget = QListWidget()
        self.list_widget.setIconSize(QPixmap(100, 100).size())
        self.layout.addWidget(self.list_widget)

        # Settings panel
        self.settings_panel = QVBoxLayout()
        self.add_settings_combos()
        self.layout.addLayout(self.settings_panel)

        # Buttons
        button_layout = QHBoxLayout()
        self.fetch_button = QPushButton("Fetch Sketches")
        self.add_local_button = QPushButton("Add Local Sketch")
        self.compile_button = QPushButton("Compile Selected Sketch")
        button_layout.addWidget(self.fetch_button)
        button_layout.addWidget(self.add_local_button)
        button_layout.addWidget(self.compile_button)
        self.layout.addLayout(button_layout)

        # Status label
        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("font-weight: bold; color: blue;")
        self.layout.addWidget(self.status_label)

        # Connections
        self.fetch_button.clicked.connect(self.fetch_sketches)
        self.add_local_button.clicked.connect(self.add_local_sketch)
        self.compile_button.clicked.connect(self.compile_sketch)

        # Data
        self.sketches = []

    
    def add_section_header(self, text):
        """Add a styled section header to the list widget."""
        header = QListWidgetItem(text)
        header.setFlags(Qt.ItemFlag.NoItemFlags)  # Make non-selectable
        font = QFont()
        font.setBold(True)
        header.setFont(font)
        header.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setBackground(QColor(240, 240, 240))  # Light gray background
        self.list_widget.addItem(header)

    def add_settings_combos(self):
        """Add settings combo boxes with consistent naming."""
        settings = {
            "Variant": [
                "Arduino Leonardo",
                "Arduino/Genuino Micro",
                "Pro Micro 5V Standard Wiring",
                "Arduino Pro Micro Alternate Wiring"
            ],
            "Display": ["SH1106", "SSD1306", "SSD1309"],
            "Flash Chip": ["Pin2/D1/SDA", "Pin0/D0/Rx"]  # This key defines the attribute name
        }

        for name, options in settings.items():
            combo = QComboBox()
            combo.addItems(options)
            self.settings_panel.addWidget(QLabel(f"{name}:"))
            self.settings_panel.addWidget(combo)
            # Convert to snake_case for the attribute name
            attr_name = name.lower().replace(' ', '_') + '_combo'
            setattr(self, attr_name, combo)

    def resource_path(self, relative_path):
        """Get absolute path to resource."""
        try:
            base_path = sys._MEIPASS
        except Exception:
            base_path = os.path.abspath(".")
        return os.path.join(base_path, relative_path)

    def fetch_sketches(self):
        """Start fetching sketches from all sources."""
        self.fetch_thread = FetchThread()
        self.fetch_thread.fetched.connect(self.populate_list)
        self.fetch_thread.start()

    
    def populate_list(self, new_sketches):
        """Rebuild the list with categorized sections."""
        # Clear existing items
        self.list_widget.clear()

        # Categorize sketches
        eried_sketches = []
        cart_builder_sketches = []
        local_sketches = []

        # Separate new sketches
        for sketch in new_sketches:
            if sketch.get('type') == 'bigfx':
                cart_builder_sketches.append(sketch)
            else:
                eried_sketches.append(sketch)

        # Get existing local sketches
        local_sketches = [
            self.list_widget.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list_widget.count())
            if self.list_widget.item(i).data(Qt.ItemDataRole.UserRole).get("type") == "local"
        ]

        # Add Eried's Repo section
        if eried_sketches:
            self.add_section_header("Eried's Repo:")
            for sketch in eried_sketches:
                self.add_sketch_item(sketch)

        # Add Cart Builder section
        if cart_builder_sketches:
            self.add_section_header("Cart Builder:")
            for sketch in cart_builder_sketches:
                self.add_sketch_item(sketch)

        # Add Local Sketches section
        if local_sketches:
            self.add_section_header("Local Sketches:")
            for sketch in local_sketches:
                self.add_sketch_item(sketch)

    def add_sketch_item(self, sketch):
        """Add individual sketch item to list with proper styling."""
        item = QListWidgetItem(sketch.get("title", "Untitled"))
        item.setData(Qt.ItemDataRole.UserRole, sketch)
        
        # Style differently for local sketches
        if sketch.get('type') == 'local':
            item.setForeground(QColor(0, 128, 0))  # Green text
            font = QFont()
            font.setItalic(True)
            item.setFont(font)
        
        if "thumbnailUrl" in sketch:
            self.load_thumbnail(item, sketch["thumbnailUrl"])
            
        self.list_widget.addItem(item)

    def load_thumbnail(self, item, url):
        """Load thumbnail image asynchronously with validation."""
        if not url or not url.startswith(('http:', 'https:')):
            return  # Skip invalid URLs
            
        def set_thumbnail(data):
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            item.setIcon(QIcon(pixmap))
            
        try:
            thread = requests.get(url, stream=True)
            thread.onload = lambda: set_thumbnail(thread.content)
            thread.start()
        except requests.exceptions.RequestException as e:
            print(f"Error loading thumbnail: {e}")

    def filter_sketches(self):
        """Filter list based on search text."""
        search_text = self.search_bar.text().lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            sketch = item.data(Qt.ItemDataRole.UserRole)
            text_matches = any(
                search_text in (sketch.get(field, "") or "").lower()
                for field in ["title", "description"]
            )
            item.setHidden(not text_matches)

    # ... [Keep existing methods for get_build_flags, add_local_sketch, 
    # compile_sketch, handle compilation, etc. from previous version] ...

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
        flash_chip = self.flash_chip_combo.currentText()  # Corrected attribute name

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
        }.get(flash_chip, "-DCART_CS_SDA")  # Changed variable name here

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
