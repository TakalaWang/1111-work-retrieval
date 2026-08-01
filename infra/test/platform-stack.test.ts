import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { describe, expect, test } from 'vitest';

import { PlatformStack } from '../lib/platform-stack.ts';

const app = new App();
const template = Template.fromStack(
  new PlatformStack(app, 'TestPlatform', {
    env: { account: '111111111111', region: 'us-east-1' }
  })
);

describe('platform stack', () => {
  test('requires immutable deployment inputs and defaults GPU capacity to zero', () => {
    template.hasParameter('ApiImageUri', {
      Type: 'String',
      AllowedPattern:
        '^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$'
    });
    template.hasParameter('ArtifactManifestSha256', {
      Type: 'String',
      AllowedPattern: '^[a-f0-9]{64}$'
    });
    template.hasParameter('GpuInstanceType', { Type: 'String' });
    for (const id of [
      'GpuMinCapacity',
      'GpuMaxCapacity',
      'GpuServiceDesiredCount'
    ]) {
      template.hasParameter(id, { Type: 'Number', Default: 0, MinValue: 0 });
    }
    template.hasResourceProperties('AWS::AutoScaling::AutoScalingGroup', {
      MinSize: { Ref: 'GpuMinCapacity' },
      MaxSize: { Ref: 'GpuMaxCapacity' },
      DesiredCapacity: Match.absent()
    });
    template.hasResourceProperties('AWS::ECS::Service', {
      DesiredCount: { Ref: 'GpuServiceDesiredCount' }
    });
  });

  test('keeps artifacts private and immutable', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          { ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } }
        ]
      },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true
      },
      VersioningConfiguration: { Status: 'Enabled' }
    });
  });

  test('uses encrypted private Aurora PostgreSQL Serverless v2', () => {
    template.hasResourceProperties('AWS::RDS::DBCluster', {
      DatabaseName: 'work_retrieval',
      DeletionProtection: true,
      EnableHttpEndpoint: true,
      Engine: 'aurora-postgresql',
      ServerlessV2ScalingConfiguration: {
        MaxCapacity: 4,
        MinCapacity: 0,
        SecondsUntilAutoPause: 600
      },
      StorageEncrypted: true
    });
    template.hasResourceProperties('AWS::RDS::DBCluster', {
      AssociatedRoles: Match.arrayWith([
        Match.objectLike({ FeatureName: 's3Import' })
      ])
    });
    template.hasResourceProperties('AWS::RDS::DBInstance', {
      DBInstanceClass: 'db.serverless',
      PubliclyAccessible: false
    });
    template.hasOutput('DatabaseSecretArn', { Value: Match.anyValue() });
    const policies = JSON.stringify(template.findResources('AWS::IAM::Policy'));
    expect(policies).toContain('s3:GetObject*');
    expect(policies).toContain('s3:List*');
  });

  test('routes API traffic through CloudFront and restricts the ALB source', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        CacheBehaviors: Match.arrayWith([
          Match.objectLike({
            PathPattern: '/api/*',
            ViewerProtocolPolicy: 'https-only'
          })
        ])
      })
    });
    template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
      Description: 'CloudFront origin-facing managed prefix list only',
      IpProtocol: 'tcp',
      FromPort: 80,
      ToPort: 80,
      SourcePrefixListId: Match.anyValue(),
      CidrIp: Match.absent(),
      CidrIpv6: Match.absent()
    });
    const serialized = JSON.stringify(template.toJSON());
    expect(serialized).toContain('X-Origin-Verify');
    expect(serialized).toContain('/healthz');
    expect(serialized).toContain('/readyz');
  });

  test('provides the private endpoints required by ECS container instances', () => {
    const endpoints = JSON.stringify(
      template.findResources('AWS::EC2::VPCEndpoint')
    );
    expect(endpoints).toContain('.ecs');
    expect(endpoints).toContain('.ecs-agent');
    expect(endpoints).toContain('.ecs-telemetry');
  });

  test('locks GitHub federation to the approved production environment', () => {
    const roles = template.findResources('AWS::IAM::Role');
    expect(JSON.stringify(roles)).toContain(
      'repo:TakalaWang/1111-work-retrieval:environment:production'
    );
    const policies = JSON.stringify(template.findResources('AWS::IAM::Policy'));
    expect(policies).toContain('s3:DeleteObject');
    expect(policies).toContain('cloudfront:CreateInvalidation');
  });
});
