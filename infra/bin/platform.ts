#!/usr/bin/env node
import { App } from 'aws-cdk-lib';

import { DataStack } from '../lib/data-stack.ts';
import { PlatformStack } from '../lib/platform-stack.ts';

const app = new App();
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION
};
const data = new DataStack(app, 'WorkRetrievalData', { env });
new PlatformStack(app, 'WorkRetrievalPlatform', {
  apiRepository: data.apiRepository,
  cluster: data.cluster,
  databaseSecurityGroup: data.databaseSecurityGroup,
  env,
  runtimeBucket: data.runtimeBucket,
  vpc: data.vpc
});
