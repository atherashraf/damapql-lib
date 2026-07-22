import base64
import os

from cryptography.fernet import Fernet



from dotenv import load_dotenv

from app.damapql.settings import DCH_BASE_DIR, DCH_CRYPTO_KEY

env_path = os.path.join(DCH_BASE_DIR, ".env")
load_dotenv(env_path)
class CryptoUtils:
    def __init__(self):

        # key = Fernet.generate_key()
        """
            generate key using
            from fernet import Fernet
            Fernet.generate_key().decode("utf-8")
        """
        self.encode_scheme = 'utf-8'
        key = DCH_CRYPTO_KEY
        self.fernet_key = key.encode(self.encode_scheme)

    def decode_base64_urlsafe(self, txt):
        # Add missing padding
        padding_needed = 4 - (len(txt) % 4)
        if padding_needed and padding_needed != 4:
            txt += "=" * padding_needed
        return base64.urlsafe_b64decode(txt)

    def encrypt_txt(self, txt: str, fernet_key=None) -> str:
        if not isinstance(txt, str):
            raise TypeError("Input must be a string.")
        fernet_key = self.fernet_key if fernet_key is None else fernet_key
        cipher_suite = Fernet(fernet_key)
        encrypted_bytes = cipher_suite.encrypt(txt.encode(self.encode_scheme))
        return encrypted_bytes.decode(self.encode_scheme)

    # def encrypt_txt(self, txt,fernet_key=None):
    #     # convert integer etc to string first
    #     txt = str(txt)
    #     # key = Fernet.generate_key()
    #     # decode_key = key.decode(self.encode_scheme)
    #     # get the key from settings
    #     fernet_key = self.fernet_key if fernet_key is None else fernet_key.encode(self.encode_scheme)
    #     # fernet_key = self.key.encode(self.encode_scheme)
    #     cipher_suite = Fernet(fernet_key)  # key should be byte
    #     # #input should be byte, so convert the text to byte
    #     encrypted_text = cipher_suite.encrypt(txt.encode(self.encode_scheme))
    #     # encode to urlsafe base64 format
    #     encrypted_text = base64.urlsafe_b64encode(encrypted_text).decode("ascii")
    #     return encrypted_text

    def decrypt_txt(self, txt: str, fernet_key=None) -> str:
        if not isinstance(txt, str):
            raise TypeError("Input must be a string.")
        fernet_key = self.fernet_key if fernet_key is None else fernet_key.encode(self.encode_scheme)
        cipher_suite = Fernet(fernet_key)
        passwaord = cipher_suite.decrypt(txt.encode(self.encode_scheme)).decode(self.encode_scheme)
        return passwaord

if __name__ == "__main__":
    encode_txt = CryptoUtils().encrypt_txt("da7%6fast1@3map")
    print(encode_txt)
    decode_txt = CryptoUtils().decrypt_txt(encode_txt)
    print(decode_txt)