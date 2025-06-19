# ImageProcessor

S3 triger permisison: aws s3api put-bucket-notification-configuration --bucket masrikdahir --cli-input-json file://IAM/trigger.json
aws s3api get-bucket-notification-configuration --bucket masrikdahir-image
aws lambda get-policy --function-name ImageProcessor
sam build --no-cached; sam deploy --guided;