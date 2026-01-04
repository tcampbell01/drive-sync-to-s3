# drive-sync-to-s3

1. Make sure you are at the root level
2. Delete old build artifacts: 

```
rm -rf lambda_build
rm -f lambda_package.zip
```

3. Create a fresh build directory

```
mkdir -p lambda_build

4. Copy the lambda source code

``` 
cp lambda/app.py lambda_build/app.py
```

5. install python dependencies INTO build folder

```
python3.11 -m pip install \
  -t lambda_build/app \
  google-api-python-client \
  google-auth \
  google-auth-oauthlib \
  google-auth-httplib2
```

NOTE: -t allows you to install into the folder regardless of what directory you are in

6. Create the zip
```
cd lambda_build
zip -r ../lambda_package.zip .
cd ../../
```

7. Upload the zip to AWS lambda

8. Verify lambda handler

lambda --> configuration --> runtime setting
- should be Handler = app.handler