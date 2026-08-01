import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { describe, expect, test } from 'vitest';

import { DataStack } from '../lib/data-stack.ts';
import { PlatformStack } from '../lib/platform-stack.ts';

const app = new App();
const data = new DataStack(app, 'TestData', {
  env: { account: '111111111111', region: 'us-east-1' }
});
const template = Template.fromStack(
  new PlatformStack(app, 'TestPlatform', {
    cluster: data.cluster,
    databaseSecurityGroup: data.databaseSecurityGroup,
    env: { account: '111111111111', region: 'us-east-1' },
    runtimeBucket: data.runtimeBucket,
    vpc: data.vpc
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

  test('reuses the data plane without duplicating persistent resources', () => {
    template.resourceCountIs('AWS::RDS::DBCluster', 0);
    template.resourceCountIs('AWS::RDS::DBInstance', 0);
    template.resourceCountIs('AWS::S3::Bucket', 1);
    const endpoints = Object.values(
      template.findResources('AWS::EC2::VPCEndpoint')
    );
    expect(endpoints).toHaveLength(7);
    expect(endpoints).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          Properties: expect.objectContaining({ VpcEndpointType: 'Interface' })
        })
      ])
    );
    expect(endpoints).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          Properties: expect.objectContaining({ VpcEndpointType: 'Gateway' })
        })
      ])
    );
    template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
      Description: 'ECS tasks only',
      FromPort: 5432,
      ToPort: 5432
    });
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
    const githubRoleId = Object.keys(roles).find((id) =>
      JSON.stringify(roles[id]).includes(
        'repo:TakalaWang/1111-work-retrieval:environment:production'
      )
    );
    expect(githubRoleId).toBeDefined();
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'ec2:DescribeManagedPrefixLists',
            Effect: 'Allow',
            Resource: '*'
          })
        ])
      },
      Roles: Match.arrayWith([{ Ref: githubRoleId }])
    });
    const policies = JSON.stringify(template.findResources('AWS::IAM::Policy'));
    expect(policies).toContain('s3:DeleteObject');
    expect(policies).toContain('cloudfront:CreateInvalidation');
  });
});
