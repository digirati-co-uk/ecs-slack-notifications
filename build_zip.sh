#! /bin/bash

# optional version suffix for zip name, defaults to timestamp
version=${1:-`date +%Y%m%d%H%M`}

echo installing requirements..
pip install -r requirements.txt --target ./package

echo copying python files..
cp *.py ./package

echo creating zip archive..
cd package && zip -r9 ../ecs-slack-notifications-$version.zip .
