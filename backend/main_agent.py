# main.py

from orchestrator.orchestrator import send_message

# EXAMPLE: generate a method for a textbox
element_spec = {
    "ocr_type": "textbox",
    "label_text": "Username",
    "unique_name": "saucedemo_login_username_textbox"
}

response = send_message("python", "generate_method", element_spec)
print("[Python Agent] Generated Method:")
print(response.payload)

# EXAMPLE: generate a test
test_case_spec = {
    "steps": [
        "enter_username(page, 'standard_user')",
        "enter_password(page, 'secret_sauce')",
        "click_login(page)"
    ]
}

test_response = send_message("python", "generate_test", test_case_spec)
print("\n[Python Agent] Generated Test:")
print(test_response.payload)
