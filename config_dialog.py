import json
import uuid
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget, QFormLayout,
    QLineEdit, QPushButton, QComboBox, QLabel, QDialogButtonBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QPlainTextEdit,
    QMessageBox, QGroupBox, QRadioButton, QButtonGroup, QCheckBox,
)
from PyQt6.QtCore import Qt
from aqt import mw
from .openrouter_client import OpenRouterClient
from .backend_client import BackendClient, AuthError
from .auth_dialog import AuthDialog

CONFIG_API_KEY = "openrouter_api_key"
CONFIG_DIRECT_API_URL = "direct_api_url"
CONFIG_DIRECT_API_KEY = "direct_api_key"
CONFIG_DIRECT_API_NO_PROXY = "direct_api_no_proxy"
CONFIG_DEFAULT_MODEL = "default_model"
CONFIG_ENABLE_STREAMING = "enable_streaming"
CONFIG_REVIEWER_ACTIONS = "reviewer_actions"
CONFIG_MODE = "mode"  # "backend", "byok", or "direct"


class ActionEditDialog(QDialog):
    def __init__(self, parent=None, action=None, note_types=None):
        super().__init__(parent)
        self.action = action or {}
        self.note_types = note_types or []

        self.setWindowTitle("Edit Reviewer Action" if action else "Add Reviewer Action")
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.id_label = QLabel(self.action.get("id", "(will be generated)"))
        form.addRow("ID:", self.id_label)

        self.button_label_edit = QLineEdit(self.action.get("button_label", ""))
        form.addRow("Button Label:", self.button_label_edit)

        self.note_type_combo = QComboBox()
        for name, mid in self.note_types:
            self.note_type_combo.addItem(name, mid)
        current = self.action.get("note_type_name")
        if current:
            idx = self.note_type_combo.findText(current)
            if idx != -1:
                self.note_type_combo.setCurrentIndex(idx)
        form.addRow("Note Type:", self.note_type_combo)

        self.target_field_combo = QComboBox()
        form.addRow("Target Field:", self.target_field_combo)

        self.prompt_edit = QPlainTextEdit(self.action.get("prompt_template", ""))
        self.prompt_edit.setMinimumHeight(120)
        self.prompt_edit.setPlaceholderText(
            "Legacy fallback prompt. Use {{FieldName}} to reference card fields.\n\n"
            "Example: Explain the word {{Front}} and give an example sentence."
        )
        form.addRow("Prompt Template (fallback):", self.prompt_edit)

        self.prompt_correct_edit = QPlainTextEdit(
            self.action.get("prompt_template_correct", "")
        )
        self.prompt_correct_edit.setMinimumHeight(120)
        self.prompt_correct_edit.setPlaceholderText(
            "Optional: prompt used when the typed answer is empty or matches the correct word.\n"
            "Leave blank to use the fallback prompt above."
        )
        form.addRow("Prompt Template (correct spelling):", self.prompt_correct_edit)

        self.prompt_incorrect_edit = QPlainTextEdit(
            self.action.get("prompt_template_incorrect", "")
        )
        self.prompt_incorrect_edit.setMinimumHeight(120)
        self.prompt_incorrect_edit.setPlaceholderText(
            "Optional: prompt used when the typed answer differs from the correct word.\n"
            "Leave blank to use the fallback prompt above."
        )
        form.addRow("Prompt Template (incorrect spelling):", self.prompt_incorrect_edit)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.note_type_combo.currentTextChanged.connect(self._update_fields)
        self._update_fields(self.note_type_combo.currentText())

    def _update_fields(self, note_type_name):
        self.target_field_combo.clear()
        if not note_type_name:
            return
        model_id = self.note_type_combo.currentData()
        if not model_id:
            model = mw.col.models.get_by_name(note_type_name)
            if not model:
                return
            model_id = model["id"]
        field_names = mw.col.models.field_names(mw.col.models.get(model_id))
        for name in field_names:
            self.target_field_combo.addItem(name)
        current = self.action.get("target_field_name")
        if current:
            idx = self.target_field_combo.findText(current)
            if idx != -1:
                self.target_field_combo.setCurrentIndex(idx)

    def get_action_data(self):
        data = {
            "id": self.action.get("id") or str(uuid.uuid4()),
            "button_label": self.button_label_edit.text().strip(),
            "note_type_name": self.note_type_combo.currentText(),
            "target_field_name": self.target_field_combo.currentText().strip(),
            "prompt_template": self.prompt_edit.toPlainText().strip(),
        }
        prompt_correct = self.prompt_correct_edit.toPlainText().strip()
        if prompt_correct:
            data["prompt_template_correct"] = prompt_correct
        prompt_incorrect = self.prompt_incorrect_edit.toPlainText().strip()
        if prompt_incorrect:
            data["prompt_template_incorrect"] = prompt_incorrect
        return data


class ConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RevAI Configuration")
        self.setMinimumWidth(700)
        self.setMinimumHeight(550)

        self.addon_package = __name__.split(".")[0]
        self.config = mw.addonManager.getConfig(self.addon_package) or {}

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self._build_api_tab()
        self._build_model_tab()
        self._build_actions_tab()

        # --- Dialog Buttons ---
        dialog_btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        dialog_btns.accepted.connect(self._save_config)
        dialog_btns.rejected.connect(self.reject)
        layout.addWidget(dialog_btns)

    # ----- Tab 1: API Config -----
    def _build_api_tab(self):
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        self.tabs.addTab(tab, "API Config")

        current_mode = self.config.get(CONFIG_MODE, "backend")

        # Mode selection
        self.mode_group = QButtonGroup(self)
        self.mode_backend = QRadioButton("RevAI Account (free credits included)")
        self.mode_byok = QRadioButton("Own API Key (use your own OpenRouter key)")
        self.mode_direct = QRadioButton("Direct API (local OpenAI-compatible endpoint)")
        self.mode_group.addButton(self.mode_backend, 0)
        self.mode_group.addButton(self.mode_byok, 1)
        self.mode_group.addButton(self.mode_direct, 2)

        if current_mode == "byok":
            self.mode_byok.setChecked(True)
        elif current_mode == "direct":
            self.mode_direct.setChecked(True)
        else:
            self.mode_backend.setChecked(True)

        tab_layout.addWidget(self.mode_backend)
        tab_layout.addWidget(self.mode_byok)
        tab_layout.addWidget(self.mode_direct)

        # --- RevAI Account section ---
        self.backend_widget = QWidget()
        backend_layout = QVBoxLayout(self.backend_widget)
        backend_layout.setContentsMargins(20, 10, 0, 0)

        auth = self.config.get("auth", {})
        email = auth.get("email", "")

        if email:
            self.account_label = QLabel(f"Signed in as: <b>{email}</b>")
            backend_layout.addWidget(self.account_label)

            status_layout = QHBoxLayout()
            self.credits_label = QLabel("Credits: loading...")
            status_layout.addWidget(self.credits_label)
            self.refresh_status_btn = QPushButton("Refresh")
            self.refresh_status_btn.clicked.connect(self._refresh_account_status)
            status_layout.addWidget(self.refresh_status_btn)
            status_layout.addStretch()
            backend_layout.addLayout(status_layout)

            # Coupon redemption
            coupon_layout = QHBoxLayout()
            self.coupon_edit = QLineEdit()
            self.coupon_edit.setPlaceholderText("Enter coupon code")
            self.coupon_edit.setMaximumWidth(200)
            coupon_layout.addWidget(self.coupon_edit)
            redeem_btn = QPushButton("Redeem")
            redeem_btn.clicked.connect(self._redeem_coupon)
            coupon_layout.addWidget(redeem_btn)
            coupon_layout.addStretch()
            backend_layout.addLayout(coupon_layout)

            logout_btn = QPushButton("Sign Out")
            logout_btn.setMaximumWidth(120)
            logout_btn.clicked.connect(self._logout)
            backend_layout.addWidget(logout_btn)

            self._refresh_account_status()
        else:
            self.account_label = QLabel("Not signed in")
            backend_layout.addWidget(self.account_label)
            login_btn = QPushButton("Sign In / Register")
            login_btn.setMaximumWidth(200)
            login_btn.clicked.connect(self._show_auth)
            backend_layout.addWidget(login_btn)

        tab_layout.addWidget(self.backend_widget)
        self.backend_widget.setVisible(current_mode == "backend")

        # --- Own API Key section ---
        self.byok_widget = QWidget()
        byok_layout = QFormLayout(self.byok_widget)
        byok_layout.setContentsMargins(20, 10, 0, 0)

        self.api_key_edit = QLineEdit(self.config.get(CONFIG_API_KEY, ""))
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-or-...")
        byok_layout.addRow("OpenRouter API Key:", self.api_key_edit)

        tab_layout.addWidget(self.byok_widget)
        self.byok_widget.setVisible(current_mode == "byok")

        # --- Direct API section ---
        self.direct_widget = QWidget()
        direct_layout = QFormLayout(self.direct_widget)
        direct_layout.setContentsMargins(20, 10, 0, 0)

        self.direct_url_edit = QLineEdit(self.config.get(CONFIG_DIRECT_API_URL, "http://127.0.0.1:8081/v1"))
        self.direct_url_edit.setPlaceholderText("http://127.0.0.1:8081/v1")
        direct_layout.addRow("API Base URL:", self.direct_url_edit)

        self.direct_key_edit = QLineEdit(self.config.get(CONFIG_DIRECT_API_KEY, ""))
        self.direct_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.direct_key_edit.setPlaceholderText("Optional")
        direct_layout.addRow("API Key (optional):", self.direct_key_edit)

        self.direct_no_proxy_check = QCheckBox("Bypass system proxy for this endpoint")
        self.direct_no_proxy_check.setChecked(self.config.get(CONFIG_DIRECT_API_NO_PROXY, True))
        direct_layout.addRow(self.direct_no_proxy_check)

        direct_hint = QLabel(
            "Use any OpenAI-compatible endpoint (e.g. local Gemini proxy, Ollama, vLLM)."
        )
        direct_hint.setStyleSheet("color: gray; font-size: 11px;")
        direct_layout.addRow(direct_hint)

        # Fetch models from the configured Direct API endpoint
        direct_fetch_layout = QHBoxLayout()
        self.direct_fetch_btn = QPushButton("Fetch Supported Models")
        self.direct_fetch_btn.clicked.connect(self._fetch_direct_models)
        direct_fetch_layout.addWidget(self.direct_fetch_btn)
        direct_fetch_layout.addStretch()
        direct_layout.addRow(direct_fetch_layout)

        tab_layout.addWidget(self.direct_widget)
        self.direct_widget.setVisible(current_mode == "direct")

        self.mode_group.buttonClicked.connect(self._on_mode_changed)

        tab_layout.addStretch()

    # ----- Tab 2: Model Config -----
    def _build_model_tab(self):
        tab = QWidget()
        self.model_tab = tab
        tab_layout = QVBoxLayout(tab)
        self.tabs.addTab(tab, "Model Config")

        fetch_layout = QHBoxLayout()
        self.fetch_btn = QPushButton("Fetch Available Models")
        self.fetch_btn.clicked.connect(self._fetch_models)
        fetch_layout.addWidget(self.fetch_btn)
        fetch_layout.addStretch()
        tab_layout.addLayout(fetch_layout)

        form = QFormLayout()
        self.model_combo = QComboBox()
        form.addRow("Default LLM Model:", self.model_combo)

        self.enable_streaming_check = QCheckBox("Stream output token-by-token (like the RevAI website)")
        self.enable_streaming_check.setChecked(self.config.get(CONFIG_ENABLE_STREAMING, True))
        form.addRow(self.enable_streaming_check)

        tab_layout.addLayout(form)

        self.model_hint = QLabel(
            "Models are fetched from the selected API. For Direct API, enter the model name manually if fetch fails."
        )
        self.model_hint.setStyleSheet("color: gray; font-size: 11px; margin-top: 10px;")
        tab_layout.addWidget(self.model_hint)

        tab_layout.addStretch()

        # Auto-fetch models on open
        self._fetch_models(show_success=False)

    # ----- Tab 3: Reviewer Actions -----
    def _build_actions_tab(self):
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        self.tabs.addTab(tab, "Reviewer Actions")

        self.actions_table = QTableWidget()
        self.actions_table.setColumnCount(5)
        self.actions_table.setHorizontalHeaderLabels(
            ["ID", "Button Label", "Note Type", "Target Field", "Prompt Template"]
        )
        self.actions_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.actions_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.actions_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tab_layout.addWidget(self.actions_table)

        btn_layout = QHBoxLayout()
        for label, handler in [
            ("Add Action", self._add_action),
            ("Edit Selected", self._edit_action),
            ("Remove Selected", self._remove_action),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            btn_layout.addWidget(btn)
        tab_layout.addLayout(btn_layout)
        self._load_actions()

    # ----- Mode toggle -----
    def _on_mode_changed(self, button):
        is_byok = button == self.mode_byok
        is_direct = button == self.mode_direct
        self.byok_widget.setVisible(is_byok)
        self.direct_widget.setVisible(is_direct)
        self.backend_widget.setVisible(not is_byok and not is_direct)

    # ----- Auth -----
    def _show_auth(self):
        dialog = AuthDialog(self)
        if dialog.exec():
            self.reject()
            ConfigDialog(mw).exec()

    def _logout(self):
        reply = QMessageBox.question(
            self, "Sign Out", "Are you sure you want to sign out?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.config["auth"] = {"access_token": "", "refresh_token": "", "email": ""}
            mw.addonManager.writeConfig(self.addon_package, self.config)
            self.reject()
            ConfigDialog(mw).exec()

    def _redeem_coupon(self):
        code = self.coupon_edit.text().strip()
        if not code:
            QMessageBox.warning(self, "Coupon", "Please enter a coupon code.")
            return

        auth = self.config.get("auth", {})
        try:
            client = BackendClient(auth.get("access_token"), auth.get("refresh_token"))
            message, new_credits = client.redeem_coupon(code)
            QMessageBox.information(self, "Coupon Redeemed", message)
            self.coupon_edit.clear()
            self._refresh_account_status()
        except Exception as e:
            QMessageBox.warning(self, "Coupon Error", str(e))

    def _refresh_account_status(self):
        auth = self.config.get("auth", {})
        access_token = auth.get("access_token")
        refresh_token = auth.get("refresh_token")
        if not access_token:
            self.credits_label.setText("Not signed in")
            return

        try:
            client = BackendClient(access_token, refresh_token)
            status = client.get_user_status()
            monthly = status.get("monthly_credits", 0)
            bonus = status.get("bonus_credits", 0)
            total = monthly + bonus
            self.credits_label.setText(
                f"Credits: {total} ({monthly} monthly + {bonus} bonus)\n"
                f"Monthly credits reset to 300 on the 1st of each month. "
                f"Bonus credits from coupons never expire."
            )

            if client.access_token != access_token:
                self.config["auth"]["access_token"] = client.access_token
                self.config["auth"]["refresh_token"] = client.refresh_token
                mw.addonManager.writeConfig(self.addon_package, self.config)
        except Exception as e:
            self.credits_label.setText(f"Could not load: {e}")

    # ----- Models -----
    DEFAULT_MODEL = "google/gemini-3.1-flash-lite-preview"

    def _fetch_models(self, show_success=True):
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("Fetching...")
        try:
            if self.config.get(CONFIG_MODE) == "direct":
                base_url = self.config.get(CONFIG_DIRECT_API_URL, "http://127.0.0.1:8081/v1")
                api_key = self.config.get(CONFIG_DIRECT_API_KEY, "")
                no_proxy = self.config.get(CONFIG_DIRECT_API_NO_PROXY, True)
                models = OpenRouterClient(api_key, base_url=base_url, no_proxy=no_proxy).get_models()
            else:
                # OpenRouter models endpoint is public, no API key needed
                models = OpenRouterClient("").get_models()
            if models:
                self._populate_models(models)
                if show_success:
                    QMessageBox.information(self, "Done", f"Fetched {len(models)} models.")
            elif show_success:
                # Only show error if user clicked the button manually
                QMessageBox.warning(self, "Error", "No models returned. Try again.")
        except Exception as e:
            if show_success:
                QMessageBox.critical(self, "Error", f"Failed to fetch models: {e}")
        finally:
            self.fetch_btn.setText("Fetch Available Models")
            self.fetch_btn.setEnabled(True)

    def _fetch_direct_models(self):
        """Fetch available models from the Direct API endpoint currently entered in the form."""
        url = self.direct_url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "Error", "Please enter the Direct API Base URL first.")
            return

        api_key = self.direct_key_edit.text().strip()
        no_proxy = self.direct_no_proxy_check.isChecked()

        self.direct_fetch_btn.setEnabled(False)
        self.direct_fetch_btn.setText("Fetching...")
        try:
            client = OpenRouterClient(api_key, base_url=url, no_proxy=no_proxy)
            models = client.get_models()
            if models:
                self._populate_models(models)
                # Switch to Model Config tab so the user sees the list
                model_tab_index = self.tabs.indexOf(self.model_tab)
                if model_tab_index >= 0:
                    self.tabs.setCurrentIndex(model_tab_index)
                QMessageBox.information(self, "Done", f"Fetched {len(models)} models.")
            else:
                QMessageBox.warning(self, "Error", "No models returned. Check the URL and API key.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to fetch models: {e}")
        finally:
            self.direct_fetch_btn.setText("Fetch Supported Models")
            self.direct_fetch_btn.setEnabled(True)

    def _populate_models(self, models_data):
        self.model_combo.clear()
        current = self.config.get(CONFIG_DEFAULT_MODEL, "") or self.DEFAULT_MODEL

        sorted_models = sorted(
            models_data,
            key=lambda x: x.get("id", "").lower() if isinstance(x, dict) else str(x).lower(),
        )

        selected_idx = -1
        combo_idx = 0
        for info in sorted_models:
            if isinstance(info, dict):
                mid = info.get("id", "")
                name = info.get("name", mid)
            else:
                mid = name = str(info)
            if not mid:
                continue
            self.model_combo.addItem(f"{name} ({mid})", mid)
            if mid == current:
                selected_idx = combo_idx
            combo_idx += 1

        if self.model_combo.count() > 0:
            if selected_idx >= 0:
                self.model_combo.setCurrentIndex(selected_idx)
            else:
                # Saved model not found in list — try to find default model
                for i in range(self.model_combo.count()):
                    if self.model_combo.itemData(i) == self.DEFAULT_MODEL:
                        self.model_combo.setCurrentIndex(i)
                        return
                # Default not found either — just use first item
                self.model_combo.setCurrentIndex(0)

    # ----- Actions -----
    def _load_actions(self):
        self.actions_table.setRowCount(0)
        for row, a in enumerate(self.config.get(CONFIG_REVIEWER_ACTIONS, [])):
            self.actions_table.insertRow(row)
            for col, key in enumerate(
                ["id", "button_label", "note_type_name", "target_field_name", "prompt_template"]
            ):
                self.actions_table.setItem(row, col, QTableWidgetItem(a.get(key, "")))

    def _add_action(self):
        note_types = self._get_note_types()
        if not note_types:
            QMessageBox.warning(self, "Error", "No note types found.")
            return
        dialog = ActionEditDialog(self, note_types=note_types)
        if dialog.exec():
            data = dialog.get_action_data()
            if not all([data["button_label"], data["note_type_name"],
                        data["target_field_name"], data["prompt_template"]]):
                QMessageBox.warning(self, "Incomplete", "All fields are required.")
                return
            self.config.setdefault(CONFIG_REVIEWER_ACTIONS, []).append(data)
            self._load_actions()

    def _edit_action(self):
        rows = self.actions_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.warning(self, "Select", "Select an action to edit.")
            return
        row_idx = rows[0].row()
        action_id = self.actions_table.item(row_idx, 0).text()
        actions = self.config.get(CONFIG_REVIEWER_ACTIONS, [])
        for i, a in enumerate(actions):
            if a.get("id") == action_id:
                dialog = ActionEditDialog(
                    self, action=a, note_types=self._get_note_types()
                )
                if dialog.exec():
                    data = dialog.get_action_data()
                    if not all([data["button_label"], data["note_type_name"],
                                data["target_field_name"], data["prompt_template"]]):
                        QMessageBox.warning(self, "Incomplete", "All fields are required.")
                        return
                    actions[i] = data
                    self._load_actions()
                return
        QMessageBox.critical(self, "Error", f"Action '{action_id}' not found.")

    def _remove_action(self):
        rows = self.actions_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.warning(self, "Select", "Select an action to remove.")
            return
        action_id = self.actions_table.item(rows[0].row(), 0).text()
        reply = QMessageBox.question(
            self, "Confirm", f"Remove action '{action_id}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            actions = self.config.get(CONFIG_REVIEWER_ACTIONS, [])
            self.config[CONFIG_REVIEWER_ACTIONS] = [
                a for a in actions if a.get("id") != action_id
            ]
            self._load_actions()

    def _get_note_types(self):
        return [(m["name"], m["id"]) for m in mw.col.models.all()]

    # ----- Save -----
    def _save_config(self):
        if self.mode_byok.isChecked():
            self.config[CONFIG_MODE] = "byok"
        elif self.mode_direct.isChecked():
            self.config[CONFIG_MODE] = "direct"
        else:
            self.config[CONFIG_MODE] = "backend"

        if hasattr(self, 'api_key_edit'):
            self.config[CONFIG_API_KEY] = self.api_key_edit.text().strip()
        if hasattr(self, 'direct_url_edit'):
            self.config[CONFIG_DIRECT_API_URL] = self.direct_url_edit.text().strip()
        if hasattr(self, 'direct_key_edit'):
            self.config[CONFIG_DIRECT_API_KEY] = self.direct_key_edit.text().strip()
        if hasattr(self, 'direct_no_proxy_check'):
            self.config[CONFIG_DIRECT_API_NO_PROXY] = self.direct_no_proxy_check.isChecked()

        if hasattr(self, 'model_combo') and self.model_combo.count() > 0:
            selected = self.model_combo.currentData()
            self.config[CONFIG_DEFAULT_MODEL] = selected or self.DEFAULT_MODEL
        elif not self.config.get(CONFIG_DEFAULT_MODEL):
            # No models fetched and nothing saved — set the default
            self.config[CONFIG_DEFAULT_MODEL] = self.DEFAULT_MODEL

        if hasattr(self, 'enable_streaming_check'):
            self.config[CONFIG_ENABLE_STREAMING] = self.enable_streaming_check.isChecked()

        mw.addonManager.writeConfig(self.addon_package, self.config)
        self.accept()
