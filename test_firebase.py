import firebase_admin
from firebase_admin import credentials, storage
import traceback
import sys

def main():
    try:
        cred_path = 'firebase_key.json'
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred, {
            'storageBucket': 'passport-photo-app-dc549.appspot.com'
        })
        bucket = storage.bucket()
        print("Checking if bucket exists...")
        if not bucket.exists():
            print("BUCKET NOT FOUND! passport-photo-app-dc549.appspot.com")
            
            # Try firebasestorage.app
            print("Trying firebasestorage.app...")
            firebase_admin.delete_app(firebase_admin.get_app())
            firebase_admin.initialize_app(cred, {
                'storageBucket': 'passport-photo-app-dc549.firebasestorage.app'
            })
            bucket2 = storage.bucket()
            if bucket2.exists():
                print("FOUND IT! It's passport-photo-app-dc549.firebasestorage.app")
            else:
                print("STILL NOT FOUND.")
        else:
            print("Bucket exists! Trying upload...")
            blob = bucket.blob("test_file.txt")
            blob.upload_from_string("hello")
            print("Upload successful!")
            try:
                blob.make_public()
                print("make_public successful!")
            except Exception as e:
                print("make_public failed!")
                traceback.print_exc()
        
    except Exception as e:
        print("Error during test:")
        traceback.print_exc()

if __name__ == "__main__":
    main()
