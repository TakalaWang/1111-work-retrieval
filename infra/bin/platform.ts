#!/usr/bin/env node
import { App } from 'aws-cdk-lib';

import { PlatformStack } from '../lib/platform-stack.ts';

const app = new App();
new PlatformStack(app, 'WorkRetrievalPlatform', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION
  }
});
