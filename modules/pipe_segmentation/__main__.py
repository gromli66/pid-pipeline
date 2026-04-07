"""
Entry point для запуска через python -m pipe_segmentation.

Примеры:
    python -m pipe_segmentation --help
    python -m pipe_segmentation prepare --help
    python -m pipe_segmentation train --data ./dataset --output ./training
"""

from pipe_segmentation.cli import main

if __name__ == '__main__':
    main()
