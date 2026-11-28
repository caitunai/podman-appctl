package cmd

import "github.com/spf13/cobra"

func Execute() error {
	root := &cobra.Command{
		Use:   "appnet",
		Short: "Manage host networking for rootless Podman containers",
		Args:  cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			return command.Help()
		},
	}

	return root.Execute()
}
