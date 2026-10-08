export interface AwsCredentials {
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken?: string;
}

export interface SignRequestInput {
  method: string;
  url: string;
  headers?: Record<string, string>;
  body?: string | Uint8Array;
  region: string;
  service: string;
  credentials: AwsCredentials;
  now?: Date;
}

export function credentialsFromEnvironment(env?: Record<string, string | undefined>): AwsCredentials | null;
export function signRequest(input: SignRequestInput): Record<string, string>;
