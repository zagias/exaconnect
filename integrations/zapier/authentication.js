const { me } = require('./lib');

module.exports = {
  type: 'custom',
  fields: [
    {
      key: 'base_url',
      label: 'Connect address',
      required: true,
      helpText: 'The address of your ExaCarib portal, such as https://connect.example.com.',
    },
    {
      key: 'api_key',
      label: 'API key',
      required: true,
      type: 'password',
      helpText: 'Create a Jibsy API key in the portal under Admin, API keys. It starts with exa_.',
    },
  ],
  test: async (z, bundle) => me(z, bundle),
  connectionLabel: (z, bundle) => bundle.inputData.email || 'Jibsy',
};
