from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QLabel, QStackedWidget,
    QMessageBox, QWidget,
)
from PyQt6.QtCore import Qt
from aqt import mw

from .backend_client import BackendClient, AuthError

ADDON_PACKAGE = __name__.split(".")[0]


class AuthDialog(QDialog):
    """Login / Register dialog for RevAI backend."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RevAI — Sign In")
        self.setMinimumWidth(400)
        self.setMaximumWidth(500)

        self.client = BackendClient()
        self.result_tokens = None  # Set on successful auth

        layout = QVBoxLayout(self)

        # Header
        header = QLabel("RevAI")
        header.setStyleSheet("font-size: 20px; font-weight: bold; margin-bottom: 5px;")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header)

        subtitle = QLabel("Sign in to get 300 free AI generations per month")
        subtitle.setStyleSheet("color: gray; margin-bottom: 15px;")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        # Stacked widget for login/register views
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        # --- Login page ---
        login_page = QWidget()
        login_layout = QVBoxLayout(login_page)
        login_form = QFormLayout()

        self.login_email = QLineEdit()
        self.login_email.setPlaceholderText("you@example.com")
        login_form.addRow("Email:", self.login_email)

        self.login_password = QLineEdit()
        self.login_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_password.setPlaceholderText("Password")
        login_form.addRow("Password:", self.login_password)

        login_layout.addLayout(login_form)

        self.login_btn = QPushButton("Sign In")
        self.login_btn.setStyleSheet("padding: 8px; font-weight: bold;")
        self.login_btn.clicked.connect(self._do_login)
        login_layout.addWidget(self.login_btn)

        self.login_status = QLabel("")
        self.login_status.setStyleSheet("color: red;")
        self.login_status.setWordWrap(True)
        login_layout.addWidget(self.login_status)

        switch_to_register = QPushButton("Don't have an account? Register")
        switch_to_register.setFlat(True)
        switch_to_register.setStyleSheet("color: #4a90d9; border: none;")
        switch_to_register.clicked.connect(lambda: self.stack.setCurrentIndex(1))
        login_layout.addWidget(switch_to_register)

        self.stack.addWidget(login_page)

        # --- Register page ---
        register_page = QWidget()
        register_layout = QVBoxLayout(register_page)
        register_form = QFormLayout()

        self.register_email = QLineEdit()
        self.register_email.setPlaceholderText("you@example.com")
        register_form.addRow("Email:", self.register_email)

        self.register_password = QLineEdit()
        self.register_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.register_password.setPlaceholderText("At least 6 characters")
        register_form.addRow("Password:", self.register_password)

        self.register_password_confirm = QLineEdit()
        self.register_password_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.register_password_confirm.setPlaceholderText("Confirm password")
        register_form.addRow("Confirm:", self.register_password_confirm)

        register_layout.addLayout(register_form)

        self.register_btn = QPushButton("Create Account")
        self.register_btn.setStyleSheet("padding: 8px; font-weight: bold;")
        self.register_btn.clicked.connect(self._do_register)
        register_layout.addWidget(self.register_btn)

        self.register_status = QLabel("")
        self.register_status.setStyleSheet("color: red;")
        self.register_status.setWordWrap(True)
        register_layout.addWidget(self.register_status)

        switch_to_login = QPushButton("Already have an account? Sign in")
        switch_to_login.setFlat(True)
        switch_to_login.setStyleSheet("color: #4a90d9; border: none;")
        switch_to_login.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        register_layout.addWidget(switch_to_login)

        self.stack.addWidget(register_page)

        # Allow Enter key to submit
        self.login_password.returnPressed.connect(self._do_login)
        self.register_password_confirm.returnPressed.connect(self._do_register)

    def _do_login(self):
        email = self.login_email.text().strip()
        password = self.login_password.text()

        if not email or not password:
            self.login_status.setText("Please enter email and password.")
            return

        self.login_btn.setEnabled(False)
        self.login_btn.setText("Signing in...")
        self.login_status.setText("")

        try:
            access_token, refresh_token, email = self.client.login(email, password)
            self.result_tokens = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "email": email,
            }
            self._save_auth(access_token, refresh_token, email)
            self.accept()
        except AuthError as e:
            self.login_status.setText(str(e))
        except Exception as e:
            self.login_status.setText(f"Connection error: {e}")
        finally:
            self.login_btn.setEnabled(True)
            self.login_btn.setText("Sign In")

    def _do_register(self):
        email = self.register_email.text().strip()
        password = self.register_password.text()
        confirm = self.register_password_confirm.text()

        if not email or not password:
            self.register_status.setText("Please enter email and password.")
            return
        if password != confirm:
            self.register_status.setText("Passwords do not match.")
            return
        if len(password) < 6:
            self.register_status.setText("Password must be at least 6 characters.")
            return

        self.register_btn.setEnabled(False)
        self.register_btn.setText("Creating account...")
        self.register_status.setText("")

        try:
            access_token, refresh_token, ret_email = self.client.register(email, password)
            # If email confirmation is disabled, we get tokens immediately
            self.result_tokens = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "email": ret_email,
            }
            self._save_auth(access_token, refresh_token, ret_email)
            QMessageBox.information(
                self, "Welcome!",
                "Account created! You have 300 free AI generations per month.\n\n"
                "Configure your AI actions in Tools > RevAI Config."
            )
            self.accept()
            return
        except AuthError as e:
            error_msg = str(e)
            if "Check your email" in error_msg or "confirm" in error_msg.lower():
                # Email confirmation required — show success message and switch to login
                QMessageBox.information(
                    self, "Check Your Email",
                    "Account created! Please check your email and click the "
                    "confirmation link, then sign in."
                )
                self.login_email.setText(email)
                self.stack.setCurrentIndex(0)  # Switch to login page
            else:
                self.register_status.setText(error_msg)
        except Exception as e:
            self.register_status.setText(f"Connection error: {e}")
        finally:
            self.register_btn.setEnabled(True)
            self.register_btn.setText("Create Account")

    def _save_auth(self, access_token, refresh_token, email):
        """Save auth tokens to addon config."""
        config = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
        config["auth"] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "email": email,
        }
        mw.addonManager.writeConfig(ADDON_PACKAGE, config)
