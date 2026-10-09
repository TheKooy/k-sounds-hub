"""English Soundboard library: local batches, saved banks, optional API search."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..soundboard_library import (
    MEDIA_EXTENSIONS, SUGGESTED_BANKS, SoundEntry, active_identities, append_import,
    folder_entries, load_bank, local_entries, prepare_import, read_library,
    record_imports, save_bank, search_freesound, web_url, write_library,
)


class LibraryWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.completed.emit(self.operation(self))
        except Exception as exc:
            self.failed.emit(str(exc))


class SoundboardLibraryDialog(QDialog):
    def __init__(self, parent, service, config_dir: Path, folder='Main', on_import=None):
        super().__init__(parent)
        self.service, self.config_dir, self.on_import = service, config_dir, on_import
        self.worker = None
        self.entries = []
        self.api_page = 1
        self.setWindowTitle('Soundboard — Add sounds & Online banks')
        self.resize(820, 650)
        self.setMinimumSize(540, 440)
        self.setStyleSheet('''
            QDialog { background: #080e1b; color: #ecf7ff; }
            QWidget#localSources, QWidget#onlineSources { background: #0d1728; }
            QTabWidget::pane { border: 1px solid #285265; background: #0d1728; }
            QLabel, QCheckBox { color: #d4e8f4; }
            QPushButton, QComboBox, QLineEdit { padding: 8px; min-height: 22px;
              border: 1px solid #285265; border-radius: 8px; background: #122337; color: #ecf7ff; }
            QPushButton:hover { background: #20435b; }
            QPushButton:disabled { color: #637e92; }
            QTreeWidget { background: #0d1728; color: #ecf7ff; border: 1px solid #285265; }
            QTreeWidget::item { padding: 6px; }
            QTreeWidget::item:selected { background: #235776; }
            QHeaderView::section { background: #162940; color: #d4e8f4; padding: 7px; }
            QTabBar::tab { background: #13253a; color: #bcd2e1; padding: 10px 18px; }
            QTabBar::tab:selected { background: #235776; color: white; }
        ''')
        root = QVBoxLayout(self)
        heading = QLabel('Choose sounds first. Add only your selection to the destination folder.')
        heading.setWordWrap(True)
        root.addWidget(heading)
        self.controls = QWidget()
        controls = QVBoxLayout(self.controls)
        controls.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        local = QWidget()
        local.setObjectName('localSources')
        local_row = QHBoxLayout(local)
        self.files_button = QPushButton('Add files…')
        self.files_button.clicked.connect(self._choose_files)
        self.folder_button = QPushButton('Add folder…')
        self.folder_button.clicked.connect(self._choose_folder)
        local_row.addWidget(self.files_button)
        local_row.addWidget(self.folder_button)
        local_row.addStretch()
        self.tabs.addTab(local, 'Local files')

        online = QWidget()
        online.setObjectName('onlineSources')
        online_layout = QVBoxLayout(online)
        row = QHBoxLayout()
        self.banks = QComboBox()
        self.banks.currentIndexChanged.connect(self._bank_chosen)
        row.addWidget(self.banks, 1)
        remove = QPushButton('Remove saved bank')
        remove.clicked.connect(self._remove_bank)
        row.addWidget(remove)
        online_layout.addLayout(row)
        row = QHBoxLayout()
        self.bank_url = QLineEdit()
        self.bank_url.setPlaceholderText('Paste a bank URL or a direct sound URL')
        row.addWidget(self.bank_url, 1)
        browse = QPushButton('Browse')
        browse.clicked.connect(self._browse_bank)
        row.addWidget(browse)
        save = QPushButton('Save bank')
        save.clicked.connect(self._save_bank)
        row.addWidget(save)
        website = QPushButton('Open website')
        website.clicked.connect(lambda: self._open_url(self.bank_url.text()))
        row.addWidget(website)
        online_layout.addLayout(row)
        help_label = QLabel('Browse reads sound lists and direct media links. Pack download pages can be saved and opened in your browser.')
        help_label.setWordWrap(True)
        online_layout.addWidget(help_label)
        self.api_toggle = QCheckBox('Freesound API search (optional)')
        online_layout.addWidget(self.api_toggle)
        self.api_controls = QWidget()
        api_layout = QVBoxLayout(self.api_controls)
        api_layout.setContentsMargins(0, 0, 0, 0)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText('Personal API key — kept only while this window is open')
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key, 1)
        key_help = QPushButton('Get API key')
        key_help.clicked.connect(lambda: self._open_url('https://freesound.org/apiv2/apply/'))
        key_row.addWidget(key_help)
        api_layout.addLayout(key_row)
        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText('Search sounds…')
        self.query.returnPressed.connect(lambda: self._search_api(False))
        row.addWidget(self.query, 1)
        search = QPushButton('Search')
        search.clicked.connect(lambda: self._search_api(False))
        row.addWidget(search)
        more = QPushButton('Next page')
        more.clicked.connect(lambda: self._search_api(True))
        row.addWidget(more)
        api_layout.addLayout(row)
        api_help = QLabel('Imports MP3 previews. Use Open source for originals and licensing details.')
        api_help.setWordWrap(True)
        api_layout.addWidget(api_help)
        online_layout.addWidget(self.api_controls)
        self.api_controls.hide()
        self.api_toggle.toggled.connect(self.api_controls.setVisible)
        self.tabs.addTab(online, 'Online banks')
        controls.addWidget(self.tabs)
        root.addWidget(self.controls)

        self.list = QTreeWidget()
        self.list.setHeaderLabels(['Sound', 'Creator / source', 'License'])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.list.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.list.setColumnWidth(2, 160)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        root.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.select_button = QPushButton('Select all')
        self.select_button.clicked.connect(self.list.selectAll)
        row.addWidget(self.select_button)
        self.source_button = QPushButton('Open source')
        self.source_button.clicked.connect(self._open_selected_source)
        row.addWidget(self.source_button)
        row.addStretch()
        row.addWidget(QLabel('Destination folder'))
        self.destination = QComboBox()
        self.destination.setEditable(True)
        doc = service.read_document()
        folders = ['Main'] + list(doc.get('folders', [])) + [s.get('folder', '') for s in doc.get('slots', []) if isinstance(s, dict)]
        self.destination.addItems(list(dict.fromkeys(str(f) for f in folders if f)))
        self.destination.setCurrentText(str(folder or 'Main'))
        row.addWidget(self.destination)
        root.addLayout(row)
        self.status = QLabel('Select multiple sounds with Ctrl or Shift, or use Select all.')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        row = QHBoxLayout()
        self.cancel = QPushButton('Cancel task')
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self._cancel_task)
        row.addWidget(self.cancel)
        row.addStretch()
        self.add_button = QPushButton('Add selected sounds')
        self.add_button.clicked.connect(self._import_selected)
        self.add_button.setEnabled(False)
        row.addWidget(self.add_button)
        close = QPushButton('Close')
        close.clicked.connect(self.reject)
        row.addWidget(close)
        root.addLayout(row)
        self._populate_banks()

    def _populate_banks(self):
        self.banks.blockSignals(True)
        self.banks.clear()
        self.banks.addItem('Choose a saved bank or a suggestion…', '')
        for bank in read_library(self.config_dir).get('banks', []):
            self.banks.addItem('Saved · ' + bank['name'], bank['url'])
        for bank in SUGGESTED_BANKS:
            self.banks.addItem('Suggested · ' + bank['name'], bank['url'])
        self.banks.blockSignals(False)

    def _bank_chosen(self, index):
        self.bank_url.setText(str(self.banks.itemData(index) or ''))
        for bank in SUGGESTED_BANKS:
            if bank['url'] == self.bank_url.text():
                self.status.setText(bank['note'])

    def _save_bank(self):
        try:
            url = web_url(self.bank_url.text())
            save_bank(self.config_dir, urlsplit(url).hostname or 'Bank', url)
            self._populate_banks()
            self.status.setText('Bank saved. No sounds were added to your folders.')
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))

    def _remove_bank(self):
        data = read_library(self.config_dir)
        url = self.bank_url.text().strip()
        data['banks'] = [b for b in data.get('banks', []) if b.get('url') != url]
        try:
            write_library(self.config_dir, data)
            self._populate_banks()
            self.status.setText('Saved bank removed. Active sounds are unchanged.')
        except OSError as exc:
            self.status.setText(str(exc))

    def _open_url(self, url):
        try:
            QDesktopServices.openUrl(QUrl(web_url(url)))
        except ValueError as exc:
            self.status.setText(str(exc))

    def _open_selected_source(self):
        for item in self.list.selectedItems()[:1]:
            entry = item.data(0, Qt.UserRole)
            self._open_url(entry.source or entry.url)

    def _run(self, operation, callback):
        if self.worker is not None:
            return
        self.controls.setEnabled(False)
        self.add_button.setEnabled(False)
        self.destination.setEnabled(False)
        self.list.setEnabled(False)
        self.select_button.setEnabled(False)
        self.cancel.setEnabled(True)
        self.status.setText('Working…')
        self.worker = LibraryWorker(operation, self)
        self.worker.completed.connect(callback)
        self.worker.failed.connect(self.status.setText)
        self.worker.progress.connect(self.status.setText)
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def _finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.controls.setEnabled(True)
        self.destination.setEnabled(True)
        self.list.setEnabled(True)
        self.select_button.setEnabled(True)
        self.cancel.setEnabled(False)
        self._selection_changed()

    def _cancel_task(self):
        if self.worker is not None:
            self.worker.requestInterruption()
            self.status.setText('Cancelling after the current file or request…')

    def reject(self):
        if self.worker is not None:
            self._cancel_task()
            return
        self.api_key.clear()
        super().reject()

    def _show_entries(self, entries, select=False):
        if self.worker is not None and self.worker.isInterruptionRequested():
            self.status.setText('Cancelled. No sounds were added.')
            return
        self.entries = entries
        active = active_identities(self.service.read_document(), self.config_dir)
        self.list.setUpdatesEnabled(False)
        try:
            self.list.clear()
            for entry in entries:
                added = entry.identity in active
                item = QTreeWidgetItem([entry.title + (' · Already added' if added else ''),
                                      entry.creator or entry.source or 'Local file', entry.license or 'Local file'])
                item.setData(0, Qt.UserRole, entry)
                item.setToolTip(0, entry.path or entry.url)
                item.setToolTip(2, entry.license)
                if added:
                    item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
                self.list.addTopLevelItem(item)
                if select and not added:
                    item.setSelected(True)
        finally:
            self.list.setUpdatesEnabled(True)
        self.status.setText(f'{len(entries)} sounds found. Select the sounds to add.' if entries else
                            'No direct sounds found. Open the website, download its pack, extract it, then use Add folder.')
        self._selection_changed()

    def _choose_files(self):
        chosen, _ = QFileDialog.getOpenFileNames(self, 'Add sounds', str(Path.home()),
            'Audio and video (' + ' '.join('*' + e for e in sorted(MEDIA_EXTENSIONS)) + ')')
        if chosen:
            self._run(lambda worker: local_entries(chosen), lambda entries: self._show_entries(entries, True))

    def _choose_folder(self):
        chosen = QFileDialog.getExistingDirectory(self, 'Add a folder of sounds', str(Path.home()))
        if chosen:
            self._run(lambda worker: folder_entries(chosen), lambda entries: self._show_entries(entries, True))

    def _browse_bank(self):
        url = self.bank_url.text()
        self._run(lambda worker: load_bank(url), self._show_entries)

    def _search_api(self, next_page):
        query, key = self.query.text().strip(), self.api_key.text().strip()
        if not query:
            self.status.setText('Enter a search term.')
            return
        self.api_page = self.api_page + 1 if next_page else 1
        page = self.api_page
        self._run(lambda worker: search_freesound(query, key, page), self._show_entries)

    def _selection_changed(self):
        count = len(self.list.selectedItems())
        self.add_button.setText(f'Add {count} selected sounds' if count else 'Add selected sounds')
        self.add_button.setEnabled(count > 0 and self.worker is None)
        self.source_button.setEnabled(count > 0 and self.worker is None and any(
            item.data(0, Qt.UserRole).url for item in self.list.selectedItems()))

    def _import_selected(self):
        entries = [item.data(0, Qt.UserRole) for item in self.list.selectedItems()]
        if not entries:
            return
        folder = self.destination.currentText()
        self._run(lambda worker: prepare_import(entries, self.config_dir,
            worker.isInterruptionRequested, worker.progress.emit), lambda result: self._commit(result, folder))

    def _commit(self, result, folder):
        if result['cancelled'] or (self.worker and self.worker.isInterruptionRequested()):
            self.status.setText('Cancelled. No sounds were added to your folders.')
            return
        prepared = result['ready']
        try:
            # Read after downloads finish, so mixer/Remote changes made meanwhile survive.
            document, count = append_import(self.service.read_document(), prepared, folder)
            if count:
                self.service.write_document(document)
            record_imports(self.config_dir, prepared)
        except Exception as exc:
            self.status.setText(f'Could not finish saving the import: {exc}')
            return
        if count and self.on_import:
            self.on_import()
        self._show_entries(self.entries)
        self.status.setText(f'Added {count} sounds to {folder or "Main"}. Skipped {len(prepared) - count} duplicates. '
                            + (f'{len(result["errors"])} files failed.' if result['errors'] else ''))
        if result['errors']:
            box = QMessageBox(self)
            box.setWindowTitle('Import results')
            box.setTextFormat(Qt.PlainText)
            box.setText(f'{len(result["errors"])} sounds could not be imported. Other sounds were added.')
            box.setDetailedText('\n'.join(result['errors'][:50]))
            box.exec()
