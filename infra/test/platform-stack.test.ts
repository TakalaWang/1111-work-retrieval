import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { describe, expect, test } from 'vitest';

import { DataStack } from '../lib/data-stack.ts';
import { PlatformStack } from '../lib/platform-stack.ts';

const app = new App({
  context: {
    '@aws-cdk/aws-autoscaling:generateLaunchTemplateInsteadOfLaunchConfig': true
  }
});
const data = new DataStack(app, 'TestData', {
  env: { account: '111111111111', region: 'us-east-1' }
});
const template = Template.fromStack(
  new PlatformStack(app, 'TestPlatform', {
    apiRepository: data.apiRepository,
    cluster: data.cluster,
    databaseSecurityGroup: data.databaseSecurityGroup,
    env: { account: '111111111111', region: 'us-east-1' },
    runtimeBucket: data.runtimeBucket,
    vpc: data.vpc
  })
);

const synthesized = template.toJSON();

describe('platform stack', () => {
  test('requires immutable inputs and derives mutually exclusive compute capacity', () => {
    template.hasParameter('ApiImageUri', {
      Type: 'String',
      AllowedPattern:
        '^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$'
    });
    template.hasParameter('ArtifactManifestSha256', {
      Type: 'String',
      AllowedPattern: '^[a-f0-9]{64}$'
    });
    template.hasParameter('ComputeProfile', {
      Type: 'String',
      Default: 'cpu-incumbent',
      AllowedValues: ['cpu-incumbent', 'gpu-shadow']
    });
    template.hasParameter('EnableGraph', {
      Type: 'String',
      Default: 'false',
      AllowedValues: ['false', 'true']
    });
    template.hasParameter('GpuInstanceType', { Type: 'String' });
    for (const id of [
      'CpuServiceDesiredCount',
      'GpuMinCapacity',
      'GpuMaxCapacity',
      'GpuServiceDesiredCount'
    ]) {
      expect(synthesized.Parameters).not.toHaveProperty(id);
    }
    template.hasResourceProperties('AWS::AutoScaling::AutoScalingGroup', {
      MinSize: { 'Fn::If': ['CpuIncumbentProfile', '0', '1'] },
      MaxSize: { 'Fn::If': ['CpuIncumbentProfile', '0', '2'] },
      DesiredCapacity: Match.absent()
    });
    template.hasResourceProperties('AWS::EC2::LaunchTemplate', {
      LaunchTemplateData: Match.objectLike({
        BlockDeviceMappings: Match.arrayWith([
          {
            DeviceName: '/dev/xvda',
            Ebs: Match.objectLike({
              Encrypted: true,
              VolumeSize: 100,
              VolumeType: 'gp3'
            })
          }
        ])
      })
    });
    template.resourceCountIs('AWS::AutoScaling::LaunchConfiguration', 0);
    expect(JSON.stringify(synthesized.Parameters)).toContain(
      '/aws/service/ecs/optimized-ami/amazon-linux-2023/gpu/recommended/image_id'
    );
  });

  test('routes the production ALB to only the explicitly active compute profile', () => {
    template.resourceCountIs('AWS::ECS::Service', 2);
    template.resourceCountIs('AWS::ECS::TaskDefinition', 2);
    const taskDefinitionsById = template.findResources(
      'AWS::ECS::TaskDefinition'
    );
    expect(
      Object.values(taskDefinitionsById).map(
        (taskDefinition) => taskDefinition.Properties.RuntimePlatform
      )
    ).toEqual(
      expect.arrayContaining([
        { CpuArchitecture: 'X86_64', OperatingSystemFamily: 'LINUX' },
        undefined
      ])
    );
    template.hasResourceProperties('AWS::ECS::Service', {
      DesiredCount: { 'Fn::If': ['CpuIncumbentProfile', 1, 0] },
      HealthCheckGracePeriodSeconds: 600,
      LaunchType: 'FARGATE',
      LoadBalancers: [
        Match.objectLike({ ContainerName: 'Api', ContainerPort: 8000 })
      ]
    });
    template.hasResourceProperties('AWS::ECS::Service', {
      DesiredCount: { 'Fn::If': ['CpuIncumbentProfile', 0, 1] },
      HealthCheckGracePeriodSeconds: 600,
      LaunchType: 'EC2',
      LoadBalancers: [
        Match.objectLike({ ContainerName: 'Api', ContainerPort: 8000 })
      ]
    });
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
      HealthCheckPath: '/readyz',
      Port: 8000,
      Protocol: 'HTTP',
      TargetType: 'ip'
    });

    const taskDefinitions = Object.values(
      template.findResources('AWS::ECS::TaskDefinition')
    );
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      Cpu: '2048',
      Memory: '16384',
      RequiresCompatibilities: ['FARGATE'],
      RuntimePlatform: {
        CpuArchitecture: 'X86_64',
        OperatingSystemFamily: 'LINUX'
      }
    });
    const containers = taskDefinitions.map(
      (resource) => resource.Properties.ContainerDefinitions[0]
    );
    expect(JSON.stringify(containers)).not.toContain('NVIDIA_VISIBLE_DEVICES');
    for (const container of containers) {
      expect(container.Image).toEqual({ Ref: 'ApiImageUri' });
      expect(container.Secrets).toEqual(
        expect.arrayContaining([
          expect.objectContaining({ Name: 'DB_USER' }),
          expect.objectContaining({ Name: 'DB_PASSWORD' })
        ])
      );
      expect(container.Environment).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            Name: 'ARTIFACT_BUCKET',
            Value: expect.any(Object)
          }),
          expect.objectContaining({
            Name: 'ARTIFACT_MANIFEST_SHA256',
            Value: { Ref: 'ArtifactManifestSha256' }
          }),
          expect.objectContaining({ Name: 'DB_HOST' }),
          expect.objectContaining({ Name: 'DB_PORT' }),
          expect.objectContaining({ Name: 'DB_NAME', Value: 'work_retrieval' }),
          expect.objectContaining({
            Name: 'SEARCH_RUNTIME_ROOT',
            Value: '/tmp/work-retrieval-runtime'
          }),
          expect.objectContaining({
            Name: 'SEARCH_RUNTIME_MANIFEST_PATH',
            Value: '/tmp/work-retrieval-runtime/manifest.json'
          }),
          expect.objectContaining({
            Name: 'SEARCH_PORT_FACTORY',
            Value: 'work_retrieval_api.production:create_production_ports'
          }),
          expect.objectContaining({
            Name: 'SEARCH_ENABLE_MULTIVIEW_MAXSIM',
            Value: 'false'
          }),
          expect.objectContaining({
            Name: 'EMBEDDING_ENDPOINT_NAME',
            Value: 'qwen3-embedding-8b-20260801-031826'
          }),
          expect.objectContaining({
            Name: 'EMBEDDING_ENDPOINT_CONFIG_NAME',
            Value: 'qwen3-embedding-8b-20260801-031826'
          }),
          expect.objectContaining({
            Name: 'EMBEDDING_MODEL_NAME',
            Value: 'qwen3-embedding-8b-20260801-031826'
          }),
          expect.objectContaining({
            Name: 'SEARCH_ENABLE_DENSE_SHADOW',
            Value: 'true'
          }),
          expect.objectContaining({
            Name: 'SEARCH_ENABLE_RERANKER',
            Value: 'active'
          }),
          expect.objectContaining({
            Name: 'SEARCH_DEMO_AS_OF',
            Value: '2026-06-08'
          }),
          expect.objectContaining({
            Name: 'SEARCH_ENABLE_GRAPH',
            Value: { Ref: 'EnableGraph' }
          }),
          expect.objectContaining({
            Name: 'RERANKER_ENDPOINT_NAME',
            Value: 'work-retrieval-qwen3-reranker-8b-v8-business'
          }),
          expect.objectContaining({
            Name: 'RERANKER_ENDPOINT_CONFIG_NAME',
            Value: 'work-retrieval-qwen3-reranker-8b-v8-business-g6-16xl'
          }),
          expect.objectContaining({
            Name: 'RERANKER_MODEL_NAME',
            Value: 'work-retrieval-qwen3-reranker-8b-v8-business'
          })
        ])
      );
    }
    const gpuContainer = containers.find(
      (container) => container.ResourceRequirements?.[0]?.Type === 'GPU'
    );
    expect(gpuContainer).toMatchObject({
      MemoryReservation: 12288,
      ResourceRequirements: [{ Type: 'GPU', Value: '1' }]
    });
    expect(gpuContainer?.Environment).toEqual(
      expect.arrayContaining([
        { Name: 'NVIDIA_DRIVER_CAPABILITIES', Value: 'compute,utility' }
      ])
    );
  });

  test('reuses persistent resources and creates only required private endpoints', () => {
    template.resourceCountIs('AWS::ECR::Repository', 0);
    template.resourceCountIs('AWS::RDS::DBCluster', 0);
    template.resourceCountIs('AWS::RDS::DBInstance', 0);
    template.resourceCountIs('AWS::S3::Bucket', 1);
    const endpoints = Object.values(
      template.findResources('AWS::EC2::VPCEndpoint')
    );
    expect(endpoints).toHaveLength(9);
    const endpointText = JSON.stringify(endpoints);
    for (const service of [
      '.ecr.api',
      '.ecr.dkr',
      '.logs',
      '.secretsmanager',
      '.sagemaker.runtime',
      '.sagemaker.api',
      '.ecs',
      '.ecs-agent',
      '.ecs-telemetry'
    ]) {
      expect(endpointText).toContain(service);
    }
    expect(synthesized.Conditions).toEqual({
      AlarmEmailConfigured: {
        'Fn::Not': [{ 'Fn::Equals': [{ Ref: 'AlarmEmail' }, ''] }]
      },
      CpuIncumbentProfile: {
        'Fn::Equals': [{ Ref: 'ComputeProfile' }, 'cpu-incumbent']
      }
    });
    expect(
      endpoints.every((endpoint) => endpoint.Condition === undefined)
    ).toBe(true);
    template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
      Description: 'API tasks only',
      FromPort: 5432,
      ToPort: 5432
    });
    template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
      Description: 'Load balancer to target',
      FromPort: 8000,
      IpProtocol: 'tcp',
      SourceSecurityGroupId: Match.anyValue(),
      ToPort: 8000
    });
  });

  test('grants artifact and model access only to immutable runtime resources', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy'));
    const taskPolicies = policies.filter((policy) =>
      JSON.stringify(policy).includes('sagemaker:InvokeEndpoint')
    );
    expect(taskPolicies).toHaveLength(2);
    for (const policy of taskPolicies) {
      const text = JSON.stringify(policy);
      expect(text).toContain('/runtime/');
      expect(text).toContain('ArtifactManifestSha256');
      expect(text).toContain('endpoint/qwen3-embedding-8b-20260801-031826');
      expect(text).toContain(
        'endpoint/work-retrieval-qwen3-reranker-8b-v8-business'
      );
      expect(text).toContain('sagemaker:DescribeEndpoint');
      expect(text).toContain('sagemaker:DescribeEndpointConfig');
      expect(text).toContain('sagemaker:DescribeModel');
      expect(text).toContain(
        'endpoint-config/qwen3-embedding-8b-20260801-031826'
      );
      expect(text).toContain('model/qwen3-embedding-8b-20260801-031826');
      expect(text).toContain(
        'endpoint-config/work-retrieval-qwen3-reranker-8b-v8-business-g6-16xl'
      );
      expect(text).toContain(
        'model/work-retrieval-qwen3-reranker-8b-v8-business'
      );
      expect(text).not.toContain(':endpoint/*');
    }
  });

  test('routes only CloudFront-authorized API traffic and monitors targets', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        Aliases: ['1111.takalawang.dev'],
        CacheBehaviors: Match.arrayWith([
          Match.objectLike({
            PathPattern: '/api/*',
            ViewerProtocolPolicy: 'https-only'
          }),
          Match.objectLike({ PathPattern: '/healthz' }),
          Match.objectLike({ PathPattern: '/readyz' })
        ]),
        ViewerCertificate: {
          AcmCertificateArn:
            'arn:aws:acm:us-east-1:378849533305:certificate/c76499fc-2946-41f4-bc40-3cec2859fffe',
          MinimumProtocolVersion: 'TLSv1.2_2021',
          SslSupportMethod: 'sni-only'
        }
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
    expect(JSON.stringify(synthesized)).toContain('X-Origin-Verify');
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: 'AWSManagedCommonRules',
          Statement: Match.objectLike({
            ManagedRuleGroupStatement: Match.anyValue()
          })
        }),
        Match.objectLike({
          Name: 'IpRateLimit',
          Action: { Block: {} },
          Statement: {
            RateBasedStatement: {
              AggregateKeyType: 'FORWARDED_IP',
              ForwardedIPConfig: {
                FallbackBehavior: 'MATCH',
                HeaderName: 'X-Forwarded-For'
              },
              Limit: 1000
            }
          }
        })
      ])
    });
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'HTTPCode_Target_5XX_Count',
      TreatMissingData: 'notBreaching'
    });
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'UnHealthyHostCount',
      TreatMissingData: 'notBreaching'
    });
  });

  test('optionally sends operational alarms to a confirmed email subscription', () => {
    template.hasParameter('AlarmEmail', {
      Type: 'String',
      Default: '',
      AllowedPattern: '^$|^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'
    });
    template.hasResourceProperties('AWS::SNS::Subscription', {
      Endpoint: { Ref: 'AlarmEmail' },
      Protocol: 'email',
      TopicArn: Match.anyValue()
    });
    const subscriptions = Object.values(
      template.findResources('AWS::SNS::Subscription')
    ).filter(
      (subscription) => subscription.Properties.Endpoint?.Ref === 'AlarmEmail'
    );
    expect(subscriptions).toHaveLength(1);
    expect(subscriptions.at(0)?.Condition).toBe('AlarmEmailConfigured');
    for (const alarm of Object.values(
      template.findResources('AWS::CloudWatch::Alarm')
    )) {
      expect(alarm.Properties.AlarmActions).toHaveLength(1);
    }
    template.hasOutput('OperationalAlarmTopicArn', {
      Value: Match.anyValue()
    });
  });

  test('locks GitHub federation and deployment writes to approved resources', () => {
    template.hasResourceProperties('Custom::AWSCDKOpenIdConnectProvider', {
      ClientIDList: ['sts.amazonaws.com'],
      Url: 'https://token.actions.githubusercontent.com'
    });
    const roles = template.findResources('AWS::IAM::Role');
    const immutableSubject =
      'repo:TakalaWang@50894789/1111-work-retrieval@1318865130:environment:production';
    const githubRoleId = Object.keys(roles).find((id) =>
      JSON.stringify(roles[id]).includes(immutableSubject)
    );
    expect(githubRoleId).toBeDefined();
    expect(JSON.stringify(roles)).not.toContain(
      'repo:TakalaWang/1111-work-retrieval:environment:production'
    );

    const githubPolicies = Object.values(
      template.findResources('AWS::IAM::Policy')
    ).filter((policy) =>
      policy.Properties.Roles?.some(
        (role: { Ref?: string }) => role.Ref === githubRoleId
      )
    );
    expect(githubPolicies).toHaveLength(1);
    const policyText = JSON.stringify(githubPolicies[0]);
    expect(policyText).toContain('ecr:PutImage');
    expect(policyText).toContain('ecr:GetAuthorizationToken');
    expect(policyText).toContain('ecr:DescribeImages');
    expect(policyText).toContain('ecr:DescribeImageScanFindings');
    expect(policyText).toContain('s3:PutObject');
    expect(policyText).toContain('s3:DeleteObject');
    expect(policyText).toContain('cloudfront:CreateInvalidation');
    expect(policyText).toContain('ec2:DescribeManagedPrefixLists');
    expect(policyText).toContain('/runtime/*/manifest.json');
    expect(policyText).not.toContain('repository/*');
    expect(policyText).not.toContain('role/cdk-*');
    for (const role of [
      'deploy-role',
      'file-publishing-role',
      'image-publishing-role',
      'lookup-role'
    ]) {
      expect(policyText).toContain(`cdk-hnb659fds-${role}`);
    }
  });

  test('exports all deployment and operational identifiers', () => {
    template.hasOutput('ApiBaseUrl', {
      Value: 'https://1111.takalawang.dev/api/v1'
    });
    template.hasOutput('WebUrl', {
      Value: 'https://1111.takalawang.dev'
    });
    for (const output of [
      'ApiRepositoryUri',
      'ApiBaseUrl',
      'WebUrl',
      'DistributionDomainName',
      'DistributionId',
      'AlbDnsName',
      'AlbArn',
      'ApiTargetGroupArn',
      'EcsClusterName',
      'EcsClusterArn',
      'CpuServiceName',
      'CpuServiceArn',
      'GpuServiceName',
      'GpuServiceArn',
      'GpuAutoScalingGroupName',
      'GpuAutoScalingGroupArn',
      'WebAclArn',
      'Target5xxAlarmName',
      'UnhealthyHostAlarmName',
      'GitHubDeployRoleArn',
      'WebBucketName'
    ]) {
      template.hasOutput(output, { Value: Match.anyValue() });
    }
  });
});
