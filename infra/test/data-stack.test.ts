import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { describe, expect, test } from 'vitest';

describe('data stack', () => {
  test('contains only the persistent retrieval data plane', async () => {
    const { DataStack } = await import('../lib/data-stack.ts');
    const app = new App();
    const template = Template.fromStack(
      new DataStack(app, 'WorkRetrievalData', {
        env: { account: '111111111111', region: 'us-west-2' }
      })
    );

    template.resourceCountIs('AWS::EC2::NatGateway', 0);
    template.resourceCountIs('AWS::EC2::SecurityGroupIngress', 0);
    template.resourceCountIs('AWS::S3::Bucket', 1);
    template.hasParameter('S3PrefixListId', {
      AllowedPattern: '^pl-[0-9a-f]+$',
      Type: 'String'
    });
    template.hasResourceProperties('AWS::EC2::SecurityGroupEgress', {
      CidrIp: Match.absent(),
      CidrIpv6: Match.absent(),
      Description: 'Aurora S3 import only',
      DestinationPrefixListId: { Ref: 'S3PrefixListId' },
      FromPort: 443,
      IpProtocol: 'tcp',
      ToPort: 443
    });
    template.hasResource('AWS::S3::Bucket', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
      Properties: Match.objectLike({
        BucketEncryption: Match.anyValue(),
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true
        },
        VersioningConfiguration: { Status: 'Enabled' }
      })
    });

    const endpoints = template.findResources('AWS::EC2::VPCEndpoint');
    expect(Object.values(endpoints)).toHaveLength(1);
    expect(Object.values(endpoints)[0]).toMatchObject({
      Properties: { VpcEndpointType: 'Gateway' }
    });
    expect(JSON.stringify(endpoints)).toContain('.s3');

    template.hasResourceProperties('AWS::RDS::DBCluster', {
      AssociatedRoles: Match.arrayWith([
        Match.objectLike({ FeatureName: 's3Import' })
      ]),
      DatabaseName: 'work_retrieval',
      DeletionProtection: true,
      EnableHttpEndpoint: true,
      Engine: 'aurora-postgresql',
      EngineVersion: '16.13',
      ServerlessV2ScalingConfiguration: {
        MaxCapacity: 4,
        MinCapacity: 0,
        SecondsUntilAutoPause: 600
      },
      StorageEncrypted: true
    });
    template.hasResourceProperties('AWS::RDS::DBInstance', {
      DBInstanceClass: 'db.serverless',
      PubliclyAccessible: false
    });
    template.hasResource('AWS::RDS::DBCluster', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain'
    });

    for (const output of [
      'RuntimeBucketName',
      'DatabaseClusterArn',
      'DatabaseSecretArn'
    ]) {
      template.hasOutput(output, { Value: Match.anyValue() });
    }
    expect(Object.keys(template.toJSON().Outputs)).toHaveLength(3);
    for (const resource of [
      'AWS::AutoScaling::AutoScalingGroup',
      'AWS::CloudFront::Distribution',
      'AWS::ECR::Repository',
      'AWS::ECS::Cluster',
      'AWS::ECS::Service',
      'AWS::ElasticLoadBalancingV2::LoadBalancer',
      'AWS::Logs::LogGroup',
      'AWS::WAFv2::WebACL'
    ]) {
      template.resourceCountIs(resource, 0);
    }
    expect(JSON.stringify(template.toJSON())).not.toContain(
      'token.actions.githubusercontent.com'
    );
  });
});
