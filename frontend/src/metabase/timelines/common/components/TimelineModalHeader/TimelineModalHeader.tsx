import type { ReactNode } from "react";
import { c } from "ttag";

import { ForwardRefLink } from "metabase/common/components/Link";
import { ActionIcon, Group, Icon, Menu, Modal, Stack } from "metabase/ui";

export interface TimelineModalMenuItem {
  label: string;
  to: string;
}

export interface TimelineModalMenu {
  /** The accessible name of the "⋯" button. */
  label: string;
  items: TimelineModalMenuItem[];
}

export interface TimelineModalHeaderProps {
  title?: ReactNode;
  collectionName?: string;
  /** A "⋯" menu of links, left out when it has no items. */
  menu?: TimelineModalMenu;
  onGoBack?: () => void;
}

/**
 * The header of the timeline list and details modals, for use inside
 * `Modal.Content`: the standard title and close button, plus an optional back
 * button, collection path and "⋯" menu.
 */
export const TimelineModalHeader = ({
  title,
  collectionName,
  menu,
  onGoBack,
}: TimelineModalHeaderProps) => (
  <Modal.Header>
    <Group gap="sm" wrap="nowrap" align="flex-start" flex="1 1 auto" miw={0}>
      {onGoBack && <Modal.BackButton onClick={onGoBack} />}
      <Stack gap="xs" flex="1 1 auto" miw={0}>
        <Modal.Title onClick={onGoBack}>{title}</Modal.Title>
        {collectionName && (
          <Group gap="xxs">
            {c("Refers to: 'Events' in a collection").t`in`}
            <Icon name="folder" c="text-secondary" />
            {collectionName}
          </Group>
        )}
      </Stack>
    </Group>
    <Group gap="sm" wrap="nowrap">
      {menu && menu.items.length > 0 && <TimelineModalMenuButton {...menu} />}
      <Modal.CloseButton />
    </Group>
  </Modal.Header>
);

const TimelineModalMenuButton = ({ label, items }: TimelineModalMenu) => (
  <Menu position="bottom-end" shadow="sm">
    <Menu.Target>
      <ActionIcon size="sm" variant="subtle" aria-label={label}>
        <Icon name="ellipsis" />
      </ActionIcon>
    </Menu.Target>
    <Menu.Dropdown>
      {items.map((item) => (
        <Menu.Item key={item.to} component={ForwardRefLink} to={item.to}>
          {item.label}
        </Menu.Item>
      ))}
    </Menu.Dropdown>
  </Menu>
);
